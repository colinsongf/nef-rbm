import os

import numpy as np

os.environ['THEANO_FLAGS'] = 'device=gpu, floatX=float32'
import theano
import theano.tensor as tt

dtype = theano.config.floatX


def get_cifar10():
    from skdata.cifar10.dataset import CIFAR10

    data = CIFAR10()
    data.meta

    test_mask = np.array([m['split'] == 'test' for m in data.meta])
    train_images = data._pixels[~test_mask]
    train_labels = data._labels[~test_mask]
    test_images = data._pixels[test_mask]
    test_labels = data._labels[test_mask]

    def process(images):
        # scale
        images = images.astype(dtype) / 255.

        # roll channel dimension before shape dimensions
        images = np.rollaxis(images, -1, 1)

        return images

    train_images, test_images = process(train_images), process(test_images)
    return (train_images, train_labels), (test_images, test_labels)


def get_mnist():
    from skdata.mnist.dataset import MNIST

    data = MNIST()
    data.meta

    train_images, train_labels, test_images, test_labels = [
        data.arrays[k] for k in
        ['train_images', 'train_labels', 'test_images', 'test_labels']]

    def process(images):
        # add unitary channel dimension
        images = images[:, None, :, :]

        # pad images to 32 x 32
        images2 = np.zeros(images.shape[0:2] + (32, 32), dtype=dtype)
        for image, image2 in zip(images, images2):
            image2[0, 2:-2, 2:-2] = image

        # scale
        images2 /= 255.

        return images2

    train_images, test_images = process(train_images), process(test_images)
    return (train_images, train_labels), (test_images, test_labels)


def ConvLarge_gettrain(batchsize):
    from theano import shared, function, config
    from theano.tensor import lscalar, tanh, dot, grad, log, arange
    from theano.tensor.nnet import softmax
    from theano.tensor.nnet.conv import conv2d
    from theano.tensor.signal.downsample import max_pool_2d

    sx = tt.tensor4()
    sy = tt.ivector()

    rng = np.random
    outputs = 10
    # lr = 0.1
    # lr = 1e-4
    lr = 1e-3

    # chan = 3  # CIFAR
    chan = 1  # MNIST

    r0 = 32
    r1 = 13
    r2 = 4

    # data_x.set_value(randn(n_examples, 1, 256, 256))
    w0 = shared(rng.rand(6, chan, 7, 7).astype(dtype) * np.sqrt(6 / (25.)))
    b0 = shared(np.zeros(6, dtype=dtype))
    w1 = shared(rng.rand(16, 6, 7, 7).astype(dtype) * np.sqrt(6 / (25.)))
    b1 = shared(np.zeros(16, dtype=dtype))
    vv = shared(rng.rand(16 * r2 * r2, 120).astype(dtype) * np.sqrt(6.0 / 16. / 25))
    cc = shared(np.zeros(120, dtype=dtype))
    # v = shared(np.zeros((120, outputs)))
    v = shared(rng.normal(scale=0.1, size=(120, outputs)).astype(dtype))
    c = shared(np.zeros(outputs, dtype=dtype))
    params = [w0, b0, w1, b1, v, c, vv, cc]

    c0 = tanh(conv2d(sx, w0, image_shape=(batchsize, chan, r0, r0),
                     filter_shape=(6, chan, 7, 7)) + b0.dimshuffle(0, 'x', 'x'))
    # this is not the correct leNet5 model, but it's closer to
    s0 = tanh(max_pool_2d(c0, (2, 2)))

    c1 = tanh(conv2d(s0, w1, image_shape=(batchsize, 6, r1, r1),
                     filter_shape=(16, 6, 7, 7)) + b1.dimshuffle(0, 'x', 'x'))
    s1 = tanh(max_pool_2d(c1, (2, 2)))

    p_y_given_x = softmax(dot(tanh(dot(s1.flatten(2), vv) + cc), v) + c)
    # p_y_given_x = softmax(dot(tanh(dot(s1.flatten(), vv) + cc), v) + c)
    nll = -log(p_y_given_x)[arange(sy.shape[0]), sy]
    cost = nll.mean()
    # error = tt.neq(tt.argmax(p_y_given_x, axis=1), sy).sum()

    gparams = grad(cost, params)

    train = function([sx, sy], cost,
            updates=[(p, p - lr * gp) for p, gp in zip(params, gparams)])
    return train


def ConvLayers_gettrain(batchsize, testsize, chan, n_layers=1):
    from theano import shared, function, config
    from theano.tensor import lscalar, tanh, dot, grad, log, arange
    from theano.tensor.nnet import softmax
    from theano.tensor.nnet.conv import conv2d
    from theano.tensor.signal.downsample import max_pool_2d

    sx = tt.tensor4()
    sy = tt.ivector()

    rng = np.random
    outputs = 10
    lr = 5e-2

    pool_size2 = lambda x: int(np.ceil(x / 2.))
    r0 = 32
    n0 = 6
    r1 = pool_size2(r0 - (7 - 1))
    n1 = 16
    r2 = pool_size2(r1 - (7 - 1))
    nv = r1**2 * n0 if n_layers == 1 else r2**2 * n1

    w0 = shared(rng.randn(n0, chan, 7, 7).astype(dtype) * np.sqrt(6. / 25))
    b0 = shared(np.zeros(n0, dtype=dtype))
    v = shared(rng.normal(scale=0.1, size=(nv, outputs)).astype(dtype))
    c = shared(np.zeros(outputs, dtype=dtype))
    params = [w0, b0, v, c]

    if n_layers >= 2:
        w1 = shared(rng.randn(n1, n0, 7, 7).astype(dtype) * np.sqrt(6. / 25))
        b1 = shared(np.zeros(n1, dtype=dtype))
        params.extend((w1, b1))

    def propup(size):
        c0 = conv2d(sx, w0, image_shape=(size, chan, r0, r0),
                    filter_shape=(6, chan, 7, 7))
        t0 = tanh(c0 + b0.dimshuffle(0, 'x', 'x'))
        s0 = tanh(max_pool_2d(t0, (2, 2)))
        y = s0

        if n_layers >= 2:
            c1 = conv2d(s0, w1, image_shape=(size, chan, r1, r1),
                        filter_shape=(6, chan, 7, 7))
            t1 = tanh(c1 + b1.dimshuffle(0, 'x', 'x'))
            s1 = tanh(max_pool_2d(t1, (2, 2)))
            y = s1

        return dot(y.flatten(2), v) + c

    p_y_given_x = softmax(propup(batchsize))
    nll = -log(p_y_given_x)[arange(sy.shape[0]), sy]
    cost = nll.mean()
    error = tt.neq(tt.argmax(p_y_given_x, axis=1), sy).mean()

    gparams = grad(cost, params)

    train = function([sx, sy], [cost, error],
            updates=[(p, p - lr * gp) for p, gp in zip(params, gparams)])

    # --- make test function
    y_pred = tt.argmax(propup(testsize), axis=1)
    error = tt.mean(tt.neq(y_pred, sy))
    test = function([sx, sy], error)

    return train, test


if 0:
    [train_images, train_labels], [test_images, test_labels] = get_cifar10()
else:
    [train_images, train_labels], [test_images, test_labels] = get_mnist()
chan = train_images.shape[1]

if 0:
    def show(image, ax=None):
        ax = plt.gca() if ax is None else ax
        if image.shape[0] == 1:
            ax.imshow(image[0], cmap='gray')
        else:
            ax.imshow(np.rollaxis(image, 0, image.ndim))

    import matplotlib.pyplot as plt
    plt.figure()
    show(train_images[1])
    plt.show()
    assert False

batch_size = 100
batches = train_images.reshape(-1, batch_size, *train_images.shape[1:])
batch_labels = train_labels.reshape(-1, batch_size)

test_size = 1000
test_batches = test_images.reshape(-1, test_size, *test_images.shape[1:])
test_batch_labels = test_labels.reshape(-1, test_size)

# train, test = ConvLayers_gettrain(batch_size, test_size, n_layers=1)
train, test = ConvLayers_gettrain(batch_size, test_size, chan, n_layers=2)

n_epochs = 100


for epoch in range(n_epochs):
    cost = 0.0
    error = 0.0
    for x, y in zip(batches, batch_labels):
        costi, errori = train(x, y)
        cost += costi
        error += errori
    error /= batches.shape[0]

    test_error = test(test_batches[0], test_batch_labels[0])
    print "Epoch %d: %f, %f, %f" % (epoch, cost, error, test_error)

error = np.mean([test(x, y) for x, y in zip(test_batches, test_batch_labels)])
print "Test error: %f" % error