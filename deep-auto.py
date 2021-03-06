"""
Train a deep autoencoder using the NEF for pretraining

TODO:
- Starting with statistical encoders then doing backprop on a layer causes
  problems when moving to the next layer. Could this be because the next layer's
  statistical encoders are somehow degenerate?
"""

import collections
import os
import gzip
import cPickle as pickle
import urllib

import numpy as np
import matplotlib.pyplot as plt
import scipy.optimize

# os.environ['THEANO_FLAGS'] = 'device=gpu, floatX=float32'
# os.environ['THEANO_FLAGS'] = 'mode=DEBUG_MODE'
import theano
import theano.tensor as tt
import theano.sandbox.rng_mrg

import nengo
import nengo.utils.distributions as dists

import plotting


def norm(x, **kwargs):
    return np.sqrt((x**2).sum(**kwargs))


class RBM(object):

    # --- define RBM parameters
    def __init__(self, vis_shape, n_hid,
                 encoders=None,
                 # intercepts=dists.Uniform(-1, 1),
                 intercepts=dists.Uniform(-0.5, -0.5),
                 # max_rates=dists.Uniform(150, 250),
                 # max_rates=dists.Uniform(1, 1),
                 max_rate=200,
                 mask=None, rf_shape=None, seed=None):

        if seed is None:
            seed = np.random.randint(2**31 - 1)

        vis_shape = vis_shape if isinstance(vis_shape, tuple) else (vis_shape,)
        n_vis = np.prod(vis_shape)

        rng = np.random.RandomState(seed=seed)

        # create initial parameters
        if encoders is None:
            encoders = rng.normal(size=(n_hid, n_vis))

        if isinstance(intercepts, dists.Distribution):
            intercepts = intercepts.sample(n_hid, rng=rng)

        # if isinstance(max_rates, dists.Distribution):
        #     max_rates = max_rates.sample(n_hid, rng=rng)
        max_rates = max_rate * np.ones(n_hid)

        # create initial sparsity mask
        if rf_shape is not None and mask is None:
            assert isinstance(vis_shape, tuple) and len(vis_shape) == 2
            M, N = vis_shape
            m, n = rf_shape

            # find random positions for top-left corner of each RF
            i = rng.randint(low=0, high=M-m+1, size=n_hid)
            j = rng.randint(low=0, high=N-n+1, size=n_hid)

            mask = np.zeros((n_hid, M, N), dtype='bool')
            for k in xrange(n_hid):
                mask[k, i[k]:i[k]+m, j[k]:j[k]+n] = True

            mask = mask.reshape(n_hid, n_vis)

        if mask is not None:
            encoders = encoders * mask
        encoders /= norm(encoders, axis=1, keepdims=True)

        self.tau_rc = 20e-3
        self.tau_ref = 2e-3
        neurons = nengo.LIF(tau_rc=self.tau_rc, tau_ref=self.tau_ref)
        gain, bias = neurons.gain_bias(max_rates, intercepts)

        self.vis_shape = vis_shape
        self.n_vis = n_vis
        self.n_hid = n_hid
        self.rf_shape = rf_shape
        self.seed = seed

        dtype = theano.config.floatX
        encoders = encoders.astype(dtype)
        max_rates = max_rates.astype(dtype)
        gain = gain.astype(dtype)
        bias = bias.astype(dtype)

        self.encoders = theano.shared(encoders, name='encoders')
        self.max_rates = theano.shared(max_rates, name='max_rates')
        self.gain = theano.shared(gain, name='gain')
        self.bias = theano.shared(bias, name='bias')
        self.mask = mask
        self.decoders = None

    # @classmethod
    # def load(cls, filename):
    #     with open(filename, 'rb') as f:
    #         obj = pickle.load(f)
    #     return obj

    # def save(self, filename):
    #     with open(filename, 'wb') as f:
    #         pickle.dump(self, f)

    def rates(self, x):
        dtype = theano.config.floatX
        sigma = tt.cast(0.05, dtype=dtype)
        tau_ref = tt.cast(self.tau_ref, dtype=dtype)
        tau_rc = tt.cast(self.tau_rc, dtype=dtype)

        j = self.gain * x + self.bias - 1
        j = sigma * tt.log1p(tt.exp(j / sigma))
        v = 1. / (tau_ref + tau_rc * tt.log1p(1. / j))
        return tt.switch(j > 0, v, 0.0) / self.max_rates

    def propup(self, x):
        e = tt.dot(x, self.encoders.T)
        return self.rates(e)

    def propdown(self, y):
        assert self.decoders is not None
        return tt.dot(y, self.decoders)

    @property
    def encode(self):
        data = tt.matrix('data')
        code = self.propup(data)
        return theano.function([data], code)

    @property
    def decode(self):
        code = tt.matrix('code')
        data = self.propdown(code)
        return theano.function([code], data)

    def check_params(self):
        for param in [self.encoders, self.max_rates, self.gain, self.bias, self.decoders]:
            if param is not None:
                assert np.isfinite(param.get_value()).all()

    def statistical_encoders(self, data):
        x = data - data.mean(0)
        corr = np.dot(x.T, x) / x.shape[0]

        w, v = np.linalg.eigh(corr)
        # plt.figure(1)
        # plt.clf()
        # plt.plot(w)
        # # plt.show()

        c2 = np.dot(v * w[None,:], v.T)
        # i, j = 300, 305
        # print corr[i:j,i:j]
        # print c2[i:j,i:j]
        assert np.allclose(corr, c2, atol=1e-6)

        # gamma = np.linalg.cholesky(corr)
        w = np.sqrt(np.maximum(w, 0))
        gamma = w[:,None] * v.T

        encoders = np.random.normal(size=(self.n_hid, self.n_vis))
        encoders = np.dot(encoders, gamma)

        # plt.figure(1)
        # plt.clf()
        # plotting.tile(encoders.reshape(-1, 28, 28))
        # plt.show()

        if self.mask is not None:
            encoders *= self.mask
        encoders /= norm(encoders, axis=1, keepdims=True)
        self.encoders.set_value(encoders.astype(theano.config.floatX))

    # def pretrain(self, batches, dbn=None, test_images=None,
    #              n_epochs=10, **train_params):
    def pretrain(self, images):
        acts = self.encode(images)
        solver = nengo.decoders.LstsqL2()
        decoders, info = solver(acts, images)

        decoders = decoders.astype(theano.config.floatX)
        self.decoders = theano.shared(decoders, name='decoders')

        print "Trained RBM: %0.3f" % (info['rmses'].mean())

    def backprop(self, images, rate=0.1, n_epochs=10):
        dtype = theano.config.floatX

        # params = []
        params = [self.encoders, self.bias, self.decoders]

        # --- compute backprop function
        x = tt.matrix('images')

        # compute coding error
        y = self.propdown(self.propup(x))
        rmses = tt.sqrt(tt.mean((x - y)**2, axis=1))
        error = tt.mean(rmses)

        # compute gradients
        grads = tt.grad(error, params)
        updates = collections.OrderedDict()
        for param, grad in zip(params, grads):
            updates[param] = param - tt.cast(rate, dtype) * grad

        train_dbn = theano.function([x], error, updates=updates)

        # --- perform SGD
        batches = images.reshape(-1, 20, images.shape[1])
        assert np.isfinite(batches).all()

        for epoch in range(n_epochs):
            costs = []
            for batch in batches:
                costs.append(train_dbn(batch))
                self.check_params()

            print "Epoch %d: %0.3f" % (epoch, np.mean(costs))

    # def backprop(self, images):
    #     dtype = theano.config.floatX

    #     params = [self.encoders, self.bias, self.decoders]

    #     # --- compute backprop function
    #     x = theano.shared(images.astype(dtype), name='images')

    #     # compute coding error
    #     y = self.propdown(self.propup(x))
    #     rmses = tt.sqrt(tt.mean((x - y)**2, axis=1))
    #     error = tt.mean(rmses)

    #     # compute gradients
    #     grads = tt.grad(error, params)
    #     f_df = theano.function([], [error] + grads)

    #     np_params = [param.get_value() for param in params]
    #     def split_p(p):
    #         split = []
    #         i = 0
    #         for param in np_params:
    #             split.append(p[i:i + param.size].reshape(param.shape))
    #             i += param.size
    #         return split

    #     def form_p(params):
    #         return np.hstack([param.flatten() for param in params])

    #     def f_df_wrapper(p):
    #         for param, value in zip(params, split_p(p)):
    #             param.set_value(value.astype(param.dtype))

    #         outs = f_df()
    #         cost, grads = outs[0], outs[1:]
    #         grad = form_p(grads)
    #         return cost.astype('float64'), grad.astype('float64')

    #     # run L_BFGS
    #     p0 = form_p(np_params)
    #     p_opt, mincost, info = scipy.optimize.lbfgsb.fmin_l_bfgs_b(
    #         f_df_wrapper, p0, maxfun=100, iprint=1)

    #     for param, value in zip(params, split_p(p_opt)):
    #         param.set_value(value.astype(param.dtype), borrow=False)

    def plot_rates(self):
        x = tt.matrix('x')
        y = self.rates(x)
        rates =  theano.function([x], y)
        x = np.linspace(-1, 1, 201).reshape(201, 1)
        x = np.tile(x, (1, self.n_hid))
        y = rates(x)

        plt.figure(101)
        plt.clf()
        plt.plot(x, y)
        plt.show()


class DBN(object):

    def __init__(self, rbms=None):
        self.rbms = rbms if rbms is not None else []
        # self.W = None  # classifier weights
        # self.b = None  # classifier biases

    def propup(self, images):
        codes = images
        for rbm in self.rbms:
            codes = rbm.propup(codes)
        return codes

    def propdown(self, codes):
        images = codes
        for rbm in self.rbms[::-1]:
            images = rbm.propdown(images)
        return images

    @property
    def encode(self):
        images = tt.matrix('images')
        codes = self.propup(images)
        return theano.function([images], codes)

    @property
    def decode(self):
        codes = tt.matrix('codes')
        images = self.propdown(codes)
        return theano.function([codes], images)

    @property
    def reconstruct(self):
        images = tt.matrix('images')
        recons = self.propdown(self.propup(images))
        return theano.function([images], recons)

    def backprop(self, images, rate=0.1, n_epochs=10):
        dtype = theano.config.floatX

        params = []
        for rbm in self.rbms:
            params.extend([rbm.encoders, rbm.bias, rbm.decoders])

        # --- compute backprop function
        x = tt.matrix('images')

        # compute coding error
        y = self.propdown(self.propup(x))
        rmses = tt.sqrt(tt.mean((x - y)**2, axis=1))
        error = tt.mean(rmses)

        # compute gradients
        grads = tt.grad(error, params)

        updates = collections.OrderedDict()
        for param, grad in zip(params, grads):
            updates[param] = param - tt.cast(rate, dtype) * grad

        train_dbn = theano.function([x], error, updates=updates)

        # --- perform SGD
        batches = images.reshape(-1, 20, images.shape[1])
        for epoch in range(n_epochs):
            costs = []
            for batch in batches:
                costs.append(train_dbn(batch))

            print "Epoch %d: %0.3f" % (epoch, np.mean(costs))

    # def backprop(self, images):
    #     dtype = theano.config.floatX

    #     params = []
    #     for rbm in self.rbms:
    #         params.extend([rbm.encoders, rbm.decoders])

    #     # --- compute backprop function
    #     x = theano.shared(images.astype(dtype), name='images')

    #     # compute coding error
    #     y = self.propdown(self.propup(x))
    #     rmses = tt.sqrt(tt.mean((x - y)**2, axis=1))
    #     error = tt.mean(rmses)

    #     # compute gradients
    #     grads = tt.grad(error, params)
    #     f_df = theano.function([], [error] + grads)

    #     np_params = [param.get_value() for param in params]
    #     def split_p(p):
    #         split = []
    #         i = 0
    #         for param in np_params:
    #             split.append(p[i:i + param.size].reshape(param.shape))
    #             i += param.size
    #         return split

    #     def form_p(params):
    #         return np.hstack([param.flatten() for param in params])

    #     # --- find target codes
    #     def f_df_wrapper(p):
    #         for param, value in zip(params, split_p(p)):
    #             param.set_value(value.astype(param.dtype))

    #         outs = f_df()
    #         cost, grads = outs[0], outs[1:]
    #         grad = form_p(grads)
    #         return cost.astype('float64'), grad.astype('float64')

    #     p0 = form_p(np_params)
    #     p_opt, mincost, info = scipy.optimize.lbfgsb.fmin_l_bfgs_b(
    #         f_df_wrapper, p0, maxfun=100, iprint=1)

    #     for param, value in zip(params, split_p(p_opt)):
    #         param.set_value(value.astype(param.dtype), borrow=False)

    def test_reconstruction(self, images):
        recons = self.reconstruct(images)
        rmses = np.sqrt(np.mean((images - recons)**2, axis=1))
        return rmses


# --- load the data
filename = 'mnist.pkl.gz'

if not os.path.exists(filename):
    url = 'http://deeplearning.net/data/mnist/mnist.pkl.gz'
    urllib.urlretrieve(url, filename=filename)

with gzip.open(filename, 'rb') as f:
    train, valid, test = pickle.load(f)

train_images, _ = train
valid_images, _ = valid
test_images, _ = test

for images in [train_images, valid_images, test_images]:
    images[:] = 2 * images - 1

# --- pretrain with CD
shapes = [(28, 28), 500, 200, 50]
n_layers = len(shapes) - 1
rf_shapes = [(9, 9), None, None]
assert len(rf_shapes) == n_layers

dbn = DBN()
# data = train_images[:1000]
data = train_images[:10000]
valid_images = valid_images[:1000]
for i in range(n_layers):


    rbm = RBM(shapes[i], shapes[i+1], rf_shape=rf_shapes[i])
    rbm.statistical_encoders(data)
    rbm.pretrain(data)
    # rbm.backprop(data)

    data = rbm.encode(data)

    dbn.rbms.append(rbm)
    rmses = dbn.test_reconstruction(valid_images)
    print "RBM %d error: %0.3f (%0.3f)" % (i, rmses.mean(), rmses.std())


plt.figure(99)
plt.clf()
recons = dbn.reconstruct(test_images)
plotting.compare(
    [test_images.reshape(-1, 28, 28), recons.reshape(-1, 28, 28)],
    rows=5, cols=20, vlims=(-1, 1))
plt.show()
# plt.savefig('nef.png')

dbn.backprop(train_images[:10000], n_epochs=10)

plt.figure(199)
plt.clf()
recons = dbn.reconstruct(test_images)
plotting.compare(
    [test_images.reshape(-1, 28, 28), recons.reshape(-1, 28, 28)],
    rows=5, cols=20, vlims=(-1, 1))
plt.show()
