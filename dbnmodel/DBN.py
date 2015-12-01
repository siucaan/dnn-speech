"""
"""
import os
import sys
import timeit

import numpy

import theano
import theano.tensor as T
from theano.sandbox.rng_mrg import MRG_RandomStreams

from logistic_sgd import LogisticRegression, load_data
from mlp import HiddenLayer
from rbm import RBM
from os import path

class DBN(object):
    """Deep Belief Network

    A deep belief network is obtained by stacking several RBMs on top of each
    other. The hidden layer of the RBM at layer `i` becomes the input of the
    RBM at layer `i+1`. The first layer RBM gets as input the input of the
    network, and the hidden layer of the last RBM represents the output. When
    used for classification, the DBN is treated as a MLP, by adding a logistic
    regression layer on top.
    """

    def __init__(self, numpy_rng, theano_rng=None, n_ins=784,
                 hidden_layers_sizes=[500, 500], n_outs=10):
        """This class is made to support a variable number of layers.

        :type numpy_rng: numpy.random.RandomState
        :param numpy_rng: numpy random number generator used to draw initial
                    weights

        :type theano_rng: theano.tensor.shared_randomstreams.RandomStreams
        :param theano_rng: Theano random generator; if None is given one is
                           generated based on a seed drawn from `rng`

        :type n_ins: int
        :param n_ins: dimension of the input to the DBN

        :type hidden_layers_sizes: list of ints
        :param hidden_layers_sizes: intermediate layers size, must contain
                               at least one value

        :type n_outs: int
        :param n_outs: dimension of the output of the network
        """

        self.sigmoid_layers = []
        self.rbm_layers = []
        self.params = []
        self.n_layers = len(hidden_layers_sizes)

        assert self.n_layers > 0

        if not theano_rng:
            theano_rng = MRG_RandomStreams(numpy_rng.randint(2 ** 30))

        # allocate symbolic variables for the data
        self.x = T.fmatrix('x')  # the data is presented as rasterized images
        self.y = T.ivector('y')  # the labels are presented as 1D vector
                                 # of [int] labels
        # end-snippet-1
        # The DBN is an MLP, for which all weights of intermediate
        # layers are shared with a different RBM.  We will first
        # construct the DBN as a deep multilayer perceptron, and when
        # constructing each sigmoidal layer we also construct an RBM
        # that shares weights with that layer. During pretraining we
        # will train these RBMs (which will lead to chainging the
        # weights of the MLP as well) During finetuning we will finish
        # training the DBN by doing stochastic gradient descent on the
        # MLP.

        for i in xrange(self.n_layers):
            # construct the sigmoidal layer

            # the size of the input is either the number of hidden
            # units of the layer below or the input size if we are on
            # the first layer
            if i == 0:
                input_size = n_ins
            else:
                input_size = hidden_layers_sizes[i - 1]

            # the input to this layer is either the activation of the
            # hidden layer below or the input of the DBN if you are on
            # the first layer
            if i == 0:
                layer_input = self.x
            else:
                layer_input = self.sigmoid_layers[-1].output

            sigmoid_layer = HiddenLayer(rng=numpy_rng,
                                        input=layer_input,
                                        n_in=input_size,
                                        n_out=hidden_layers_sizes[i],
                                        activation=T.nnet.sigmoid)

            # add the layer to our list of layers
            self.sigmoid_layers.append(sigmoid_layer)

            # its arguably a philosophical question...  but we are
            # going to only declare that the parameters of the
            # sigmoid_layers are parameters of the DBN. The visible
            # biases in the RBM are parameters of those RBMs, but not
            # of the DBN.
            self.params.extend(sigmoid_layer.params)

            # Construct an RBM that shared weights with this layer
            rbm_layer = RBM(numpy_rng=numpy_rng,
                            theano_rng=theano_rng,
                            input=layer_input,
                            n_visible=input_size,
                            n_hidden=hidden_layers_sizes[i],
                            W=sigmoid_layer.W,
                            hbias=sigmoid_layer.b)
            self.rbm_layers.append(rbm_layer)

        # We now need to add a logistic layer on top of the MLP
        self.logLayer = LogisticRegression(
            input=self.sigmoid_layers[-1].output,
            n_in=hidden_layers_sizes[-1],
            n_out=n_outs)
        self.params.extend(self.logLayer.params)

        # compute the cost for second phase of training, defined as the
        # negative log likelihood of the logistic regression (output) layer
        self.finetune_cost = self.logLayer.negative_log_likelihood(self.y)

        # compute the gradients with respect to the model parameters
        # symbolic variable that points to the number of errors made on the
        # minibatch given by self.x and self.y
        self.errors = self.logLayer.errors(self.y)

    def pretraining_functions(self, train_set_x, batch_size, k):
        '''Generates a list of functions, for performing one step of
        gradient descent at a given layer. The function will require
        as input the minibatch index, and to train an RBM you just
        need to iterate, calling the corresponding function on all
        minibatch indexes.

        :type train_set_x: theano.tensor.TensorType
        :param train_set_x: Shared var. that contains all datapoints used
                            for training the RBM
        :type batch_size: int
        :param batch_size: size of a [mini]batch
        :param k: number of Gibbs steps to do in CD-k / PCD-k

        '''

        # index to a [mini]batch
        index = T.lscalar('index')  # index to a minibatch
        learning_rate = T.scalar('lr')  # learning rate to use

        # number of batches
        n_batches = train_set_x.shape[0] / batch_size
        # begining of a batch, given `index`
        batch_begin = index * batch_size
        # ending of a batch given `index`
        batch_end = batch_begin + batch_size
        pretrain_fns = []
        for rbm in self.rbm_layers:

            # get the cost and the updates list
            # using CD-k here (persisent=None) for training each RBM.
            # TODO: change cost function to reconstruction error
            cost, updates = rbm.get_cost_updates(learning_rate,
                                                 persistent=None, k=k)

            # compile the theano function
            fn = theano.function(
                inputs=[index, theano.Param(learning_rate, default=0.1)],
                outputs=cost,
                updates=updates,
                givens={                    
                    self.x: train_set_x[batch_begin:batch_end]
                }
            )
            # append `fn` to the list of functions
            pretrain_fns.append(fn)

        return pretrain_fns

    def build_finetune_functions(self, train_set_x, train_set_y, valid_set_x,
                                valid_set_y , batch_size, learning_rate):
        '''Generates a function `train` that implements one step of
        finetuning, a function `validate` that computes the error on a
        batch from the validation set, and a function `test` that
        computes the error on a batch from the testing set

        :type datasets: list of pairs of theano.tensor.TensorType
        :param datasets: It is a list that contain all the datasets;
                        the has to contain three pairs, `train`,
                        `valid`, `test` in this order, where each pair
                        is formed of two Theano variables, one for the
                        datapoints, the other for the labels
        :type batch_size: int
        :param batch_size: size of a minibatch
        :type learning_rate: float
        :param learning_rate: learning rate used during finetune stage

        '''

        # compute number of minibatches for training, validation
        n_valid_batches = valid_set_x.get_value(borrow=True).shape[0]
        n_valid_batches /= batch_size

        index = T.lscalar('index')  # index to a [mini]batch

        # compute the gradients with respect to the model parameters
        gparams = T.grad(self.finetune_cost, self.params)

        # compute list of fine-tuning updates
        updates = []
        for param, gparam in zip(self.params, gparams):
            updates.append((param, param - gparam * learning_rate))

        train_fn = theano.function(
            inputs=[index],
            outputs=self.finetune_cost,
            updates=updates,
            givens={
                self.x: train_set_x[
                    index * batch_size: (index + 1) * batch_size
                ],
                self.y: train_set_y[
                    index * batch_size: (index + 1) * batch_size
                ]
            }
        )

        valid_score_i = theano.function(
            [index],
            self.errors,
            givens={
                self.x: valid_set_x[
                    index * batch_size: (index + 1) * batch_size
                ],
                self.y: valid_set_y[
                    index * batch_size: (index + 1) * batch_size
                ]
            }
        )

        # Create a function that scans the entire validation set
        def valid_score():
            return [valid_score_i(i) for i in xrange(n_valid_batches)]

        return train_fn, valid_score


class DbnClassifier:
    def __init__(self, params):
        print('----------Using DBN model with the below configuration----------') 
        print('nLayers:%d'%(len(params['hidden_layers'])))
        print('Layer sizes: [%s]'%(' '.join(map(str,params['hidden_layers']))))
        print('Dropout Prob: %.2f '%(params['drop_prob_encoder']))
        print('Pretraining epochs: %d Pretraining learning rate: %f'%(
                    params['pre_max_epochs'],params['plr']))
        print('Training epochs: %d Training learning rate: %f'%(
                    params['max_epochs'],params['lr'])) 
    def build_model(self, params):
        hidden_layers = params['hidden_layers']
        input_dim = params['feat_size']
        output_dim = params['phone_vocab_size']
        drop_prob = params['drop_prob_encoder']
        self.n_layers = len(hidden_layers)
        # numpy random generator
        numpy_rng = numpy.random.RandomState(123)
        print '... building the model'    
        self.model = DBN(numpy_rng=numpy_rng, n_ins=input_dim,
                        hidden_layers_sizes=hidden_layers,
                        n_outs=output_dim)
        
    def train_model(self, train_x, train_y, val_x, val_y, params):
        pretrain_lr=params['plr']
        pretraining_epoch = params['pre_max_epochs']
        finetune_lr=params['lr']
        k = 1
        epoch= params['max_epochs']
        batch_size=params['batch_size']
        out_dir=params['out_dir']           
        #borrow = True
        #train_set_x = theano.shared(numpy.asarray(train_x,
        #                                       dtype=theano.config.floatX),
        #                            borrow=borrow)
        #train_set_y = T.cast(theano.shared(numpy.asarray(train_y,
        #                                       dtype=theano.config.floatX),
        #                            borrow=borrow),
        #                    'int32')
        #valid_set_x = theano.shared(numpy.asarray(val_x,
        #                                       dtype=theano.config.floatX),
        #                            borrow=borrow)
        #valid_set_y = T.cast(theano.shared(numpy.asarray(val_y,
        #                                       dtype=theano.config.floatX),
        #                            borrow=borrow),
        #                    'int32')
        
        # Convert one-hot data to label to fit the DBN dataframe
        def bin_to_int(mat):
            n = numpy.shape(mat)[0]
            m = numpy.shape(mat)[1]
            res = numpy.zeros(n)
            for i in range(0, n):
                for j in range(0,m):
                    if mat[i][j] == 1:
                        res[i] = j
            return res
        train_set_x = theano.shared(value=train_x, name='train_set_x')
        train_set_y = T.cast(theano.shared(value=bin_to_int(train_y), 
                                            name='train_set_y'), 'int32')
        valid_set_x = theano.shared(value=val_x, name='valid_set_x')
        valid_set_y = T.cast(theano.shared(value=bin_to_int(val_y), 
                                            name='valid_set_y'), 'int32')
        ## pretrain ##########################################################
        print '... getting the pretraining functions'    
        pretraining_fns = self.model.pretraining_functions(train_set_x=train_set_x,
                                                    batch_size=batch_size,
                                                    k=k)
        # prepare the data in batches
        n_train_batches = train_set_x.get_value(borrow=True).shape[0] / batch_size
                
        print '... pre-training the model'
        start_time = timeit.default_timer()
        ## Pre-train layer-wise
        for i in xrange(self.model.n_layers):
            # go through pretraining epochs
            for epoch_j in xrange(pretraining_epoch):
                # go through the training set
                c = []
                for batch_index in xrange(n_train_batches):
                    c.append(pretraining_fns[i](index=batch_index,
                                                lr=pretrain_lr))
                print 'Pre-training layer %i, epoch %d, cost ' % (i, epoch_j),
                print numpy.mean(c)
    
        end_time = timeit.default_timer()
        
        ## output some info here
        
        
        ## finetune #########################################################
        print '... getting the finetuning functions'
        train_fn, validate_model = self.model.build_finetune_functions(
            train_set_x=train_set_x, train_set_y=train_set_y, 
            valid_set_x = valid_set_x, valid_set_y=valid_set_y,
            batch_size=batch_size,
            learning_rate=finetune_lr
        )
        
        print '... finetuning the model'
        # early-stopping parameters
        patience = 4 * n_train_batches  # look as this many examples regardless
        patience_increase = 2.    # wait this much longer when a new best is
                                # found
        improvement_threshold = 0.995  # a relative improvement of this much is
                                    # considered significant
        validation_frequency = min(n_train_batches, patience / 2)
                                    # go through this many
                                    # minibatches before checking the network
                                    # on the validation set; in this case we
                                    # check every epoch
        best_validation_loss = numpy.inf
        start_time = timeit.default_timer()
        done_looping = False
        epoch_j = 0                                       
    
        while (epoch_j < epoch) and (not done_looping):
            epoch_j = epoch_j + 1
            for minibatch_index in xrange(n_train_batches):
    
                minibatch_avg_cost = train_fn(minibatch_index)
                iter = (epoch_j - 1) * n_train_batches + minibatch_index
    
                if (iter + 1) % validation_frequency == 0:
    
                    validation_losses = validate_model()
                    this_validation_loss = numpy.mean(validation_losses)
                    print(
                        'epoch %i, minibatch %i/%i, validation error %f %%'
                        % (
                            epoch_j,
                            minibatch_index + 1,
                            n_train_batches,
                            this_validation_loss * 100.
                        )
                    )
    
                    # if we got the best validation score until now
                    if this_validation_loss < best_validation_loss:
    
                        #improve patience if loss improvement is good enough
                        if (
                            this_validation_loss < best_validation_loss *
                            improvement_threshold
                        ):
                            patience = max(patience, iter * patience_increase)
    
                        # save best validation score and iteration number
                        best_validation_loss = this_validation_loss
                        best_iter = iter
    
                if patience <= iter:
                    done_looping = True
                    break
    
        end_time = timeit.default_timer()
        print(
            (
                'Optimization complete with best validation score of %f %%, '
                'obtained at iteration %i, '
            ) % (best_validation_loss * 100., best_iter + 1)
        )
        fname = path.join(out_dir, 'DBN_weights_'+params['out_file_append'] +'_{val_loss:.2f}.hdf5')    
        return fname, best_validation_loss
