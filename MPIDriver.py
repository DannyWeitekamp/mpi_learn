#!/usr/bin/env python

### This script creates a Keras model and a Manager object that handles distributed training.

import sys,os
import numpy as np
import argparse
import json
from mpi4py import MPI
from time import time,sleep

from .mpi_tools.MPIManager import MPIManager, get_device
from .Algo import Algo
from .Data import H5Data

def load_model(model_name, load_weights):
    """Loads model architecture from <model_name>_arch.json.
        If load_weights is True, gets model weights from
        <model_name_weights.h5"""
    json_filename = "%s_arch.json" % model_name
    with open( json_filename ) as arch_file:
        model = model_from_json( arch_file.readline() ) 
    if load_weights:
        weights_filename = "%s_weights.h5" % model_name
        model.load_weights( weights_filename )
    return model

if __name__ == '__main__':
    parser = argparse.ArgumentParser()

    # model arguments
    parser.add_argument('model_name', help=('will load model architecture from '
                                            '<model_name>_arch.json'))
    parser.add_argument('--load-weights',help='load weights from <model_name>_weights.h5',
            action='store_true')
    parser.add_argument('--trial-name', help='descriptive name for trial', 
            default='train', dest='trial_name')

    # training data arguments
    parser.add_argument('train_data', help='text file listing data inputs for training')
    parser.add_argument('val_data', help='text file listing data inputs for validation')
    parser.add_argument('--features-name', help='name of HDF5 dataset with input features',
            default='features', dest='features_name')
    parser.add_argument('--labels-name', help='name of HDF5 dataset with output labels',
            default='labels', dest='labels_name')
    parser.add_argument('--batch', help='batch size', default=100, type=int)

    # configuration of network topology
    parser.add_argument('--masters', help='number of master processes', default=1, type=int)
    parser.add_argument('--max-gpus', dest='max_gpus', help='max GPUs to use', 
            type=int, default=-1)
    parser.add_argument('--synchronous',help='run in synchronous mode',action='store_true')

    # configuration of training process
    parser.add_argument('--epochs', help='number of training epochs', default=1, type=int)
    parser.add_argument('--optimizer',help='optimizer for master to use',default='rmsprop')
    parser.add_argument('--loss',help='loss function',default='binary_crossentropy')
    parser.add_argument('--worker-optimizer',help='optimizer for workers to use',
            dest='worker_optimizer', default='sgd')
    parser.add_argument('--sync-every', help='how often to sync weights with master', 
            default=1, type=int, dest='sync_every')
    parser.add_argument('--easgd',help='use Elastic Averaging SGD',action='store_true')
    parser.add_argument('--elastic-force',help='beta parameter for EASGD',type=float,default=0.9)
    parser.add_argument('--elastic-lr',help='worker SGD learning rate for EASGD',
            type=float, default=1.0, dest='elastic_lr')

    args = parser.parse_args()
    model_name = args.model_name

    with open(args.train_data) as train_list_file:
        train_list = [ s.strip() for s in train_list_file.readlines() ]
    with open(args.val_data) as val_list_file:
        val_list = [ s.strip() for s in val_list_file.readlines() ]

    comm = MPI.COMM_WORLD.Dup()
    # We have to assign GPUs to processes before importing Theano.
    device = get_device( comm, args.masters, gpu_limit=args.max_gpus )
    print "Process",comm.Get_rank(),"using device",device
    os.environ['THEANO_FLAGS'] = "device=%s,floatX=float32" % (device)
    import theano

    # There is an issue when multiple processes import Keras simultaneously --
    # the file .keras/keras.json is sometimes not read correctly.  
    # as a workaround, just try several times to import keras.
    # Note: importing keras imports theano -- 
    # impossible to change GPU choice after this.
    for try_num in range(10):
        try:
            from keras.models import model_from_json
            import keras.callbacks as cbks
            break
        except ValueError:
            print "Unable to import keras. Trying again: %d" % try_num
            sleep(0.1)

    # We initialize the Data object with the training data list
    # so that we can use it to count the number of training examples
    data = H5Data( train_list, batch_size=args.batch, 
            features_name=args.features_name, labels_name=args.labels_name )
    if comm.Get_rank() == 0:
        validate_every = data.count_data()/args.batch 
    callbacks = []
    callbacks.append( cbks.ModelCheckpoint( '_'.join([
        model_name,args.trial_name,"mpi_learn_result.h5"]), 
        monitor='val_loss', verbose=1 ) )

    # Creating the MPIManager object causes all needed worker and master nodes to be created
    manager = MPIManager( comm=comm, data=data, num_epochs=args.epochs, 
            train_list=train_list, val_list=val_list, num_masters=args.masters,
            synchronous=args.synchronous, callbacks=callbacks )

    # Process 0 defines the model and propagates it to the workers.
    if comm.Get_rank() == 0:
        model = load_model(model_name, load_weights=args.load_weights)
        model_arch = model.to_json()
        if args.easgd:
            algo = Algo(None, loss=args.loss, validate_every=validate_every,
                    mode='easgd', elastic_lr=args.elastic_lr, sync_every=args.sync_every,
                    worker_optimizer=args.worker_optimizer,
                    elastic_force=args.elastic_force/(comm.Get_size()-1)) 
        else:
            algo = Algo(args.optimizer, loss=args.loss, validate_every=validate_every,
                    sync_every=args.sync_every, worker_optimizer=args.worker_optimizer) 
        print algo
        weights = model.get_weights()

        manager.process.set_model_info( model_arch, algo, weights )
        t_0 = time()
        histories = manager.process.train() 
        delta_t = time() - t_0
        manager.free_comms()
        print "Training finished in %.3f seconds" % delta_t

        # Make output dictionary
        out_dict = { "args":vars(args),
                     "history":histories,
                     "train_time":delta_t,
                     }
        json_name = '_'.join([model_name,args.trial_name,"history.json"]) 
        with open( json_name, 'w') as out_file:
            out_file.write( json.dumps(out_dict, indent=4, separators=(',',': ')) )
        print "Wrote trial information to",json_name
