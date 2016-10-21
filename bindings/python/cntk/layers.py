﻿# ==============================================================================
# Copyright (c) Microsoft. All rights reserved.
# Licensed under the MIT license. See LICENSE.md file in the project root
# for full license information.
# ==============================================================================

# layers -- blocks in the network that are used layer-like, i.e. layered on top of each other
#           e.g. a fully connected layer with non-linearity

# TODO: clean up the dependencies
import numpy as np
import sys
import os
import time
from cntk import DeviceDescriptor, Trainer, Axis, text_format_minibatch_source, StreamConfiguration
from cntk.learner import sgd, fsadagrad
from cntk.ops import parameter, input_variable, placeholder_variable, combine
from cntk.ops import times, cross_entropy_with_softmax, classification_error, convolution, batch_normalization
import itertools
from cntk.utils.debughelpers import _name_node, _node_name, _node_description, _log_node
from cntk.utils import Record, _as_tuple
from cntk.blocks import *  # TODO: reduce to what we actually use
from cntk.blocks import _trace_layers  # (debugging)
from cntk.initializer import glorot_uniform
from _cntk_py import constant_initializer # BUGBUG: Should not be necessary, should just type-cast under the hood.

abs_path = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.join(abs_path, "..", ".."))
# TODO: move these out from examples
#from examples.common.nn import slice, sigmoid, log, tanh, past_value, future_value, print_training_progress, negate

from cntk.ops.functions import Function
from cntk.ops.variables import Variable

# this is what we initialize weight matrices from by default
from cntk.blocks import _default_initializer, _INFERRED

# Dense -- create a fully-connected linear projection layer with optional non-linear activation
# Note: shape may describe a tensor as well.
# input_rank given: number of inferred axes to add to W (map_rank must not be given)
# map_rank   given: expand W to leave exactly mapRank axes (input_rank must not be given)
# none       given: expand W to all (same as map_rank=0)
def Dense(shape, init=_default_initializer, activation=identity, input_rank=None, map_rank=None, bias=True, init_bias=0):
    output_shape = _as_tuple(shape)

    if input_rank is not None and map_rank is not None:
        raise ValueError("Dense: input_rank and map_rank cannot be specified at the same time.")

    # determine meaning of axes
    # W gets dimension (input_shape + shape)
    # where input_shape is determined as:
    #  - by default, equal to the dimensions of the input passed to Dense()
    #  - if input_rank is given, then the last 'input_rank' dimensions of the input (all others are not reduced over)
    #  - if map_rank is given, then the all but the first 'map_rank' dimensions of the input (those are not reduced over)
    # where input_rank and map_rank are mutuallly exclusive.

    #output_rank = -len(output_shape)   # support outputs with tensor layouts
    # BUGBUG: Should this be a negative number now, since output is the last axis in Python?
    output_rank = len(output_shape)   # support outputs with tensor layouts

    # If input_rank not given then pass a single _INFERRED; map_rank if given will determine the input_rank.
    # The dimension inference may still create multiple axes.
    input_shape = _INFERRED * (input_rank if input_rank is not None else 1)

    if input_rank is not None:
        UntestedBranchError("Dense, input_rank option not implemented")
        infer_input_rank_to_map = -1 # means map_rank is not specified; input_rank rules
    elif map_rank is None:
        infer_input_rank_to_map = 0  # neither given: default to 'infer W to use all input dims'
    else:
        UntestedBranchError("Dense, map_rank option not implemented")
        infer_input_rank_to_map = map_rank  # infer W to use all input dims except the first static 'map_rank' ones

    # parameters bound to this Function
    W = Parameter(input_shape + output_shape, init=init     , name='W')
    b = Parameter(              output_shape, init=init_bias, name='b') if bias else None

    # expression of this function
    x = Placeholder(name='dense_arg')
    apply_x = times(x, W, output_rank=output_rank, infer_input_rank_to_map=infer_input_rank_to_map)
    if b:
        apply_x = apply_x + b
    #_extend_Function(apply_x)  # (this gets us the >> operator  --TODO: remove once Function natively supports this)
    apply_x = apply_x >> activation
    #_name_and_extend_Function(apply_x, 'Dense')
    return Block(apply_x, 'Dense', Record(W=W, b=b))

# Embedding -- create a linear embedding layer
# To create an embedding from a file, use this:
#  Embedding(weights=np.load('PATH'))
def Embedding(shape=None, init=None, weights=None):
    if init is not None or weights is not None:
        raise ValueError('Embedding: init and weights options are mutually exclusive')

    # parameters bound to this Function:
    # no weights given: learn the embedding
    if weights is None:
        if shape is None:
            raise ValueError('Embedding: output shape must be specified')
        if init is None:
            init = _default_initializer
        shape = _as_tuple(shape)
        weight_shape = _INFERRED + shape
        E = Parameter(weight_shape, init=init, name='E')
    # weights given: use them as constant
    else:
        UntestedBranchError("Embedding, from constant")
        import numpy as np
        if not isinstance(weights, array): # TODO: is this the correct test for a numpy array
            UntestedBranchError("Embedding, from constant that is not an array")
            # TODO: can 'weights' be a CNTK object? Then how to do this?
            raise ValueError('Embedding: weights must be a numpy array')
        weight_shape = np.shape(weights)
        if shape is not None: # user may give shape, then it must match
            if len(shape) >= len(weight_shape) or weight_shape[-len(shape):] != shape:
                raise ValueError('Embedding: shape parameter must match weights')
        E = Constant(weights, name='E')

    # expression
    x = Placeholder(name='embedding_arg')
    apply_x = times(x, E)
    #_name_and_extend_Function(apply_x, 'Embedding')
    return Block(apply_x, 'Embedding', Record(E=E))

# helper to create a new initializer object from an existing one, while updating members
# BUGBUG: Currently not really working, only for glorot_uniform.
def _init_with(init, members):
    d = init.__dict__
    return glorot_uniform(**members) # BUGBUG: currently no way to add these fields to an existing initializer

# Convolution -- create a convolution layer with optional non-linearity
#             ( (sample shape) +  (output shape) +  (reduction shape) + (shifting shape) )
#    in     : ( (sample shape) +                 +  (reduction shape) + (shifting shape) )
#    kernel : (                +  (output shape) +  (reduction shape) + (filte  shape)   )
#    out    : ( (sample shape) +  (output shape) +                    + (shifting shape) )
# TODO: Can we specify atrous convolution? How?
# TODO: sharing = false?
# TODO: conflict of parameter order: filter_shape or num_filters first?
#  - filter_shape first is logical for non-NN applications such as straight image filtering
#  - num_filters first is what Keras does
def Convolution(filter_shape,        # e.g. (3,3)
                num_filters=None,    # e.g. 64 or None (which means 1 channel and don't add a dimension_
                activation=identity,
                init=_default_initializer,
                pad=False,
                #lowerPad=None, upperPad=None, # TODO: clean this up; leaving it out for now
                strides=1,
                sharing=True,     # (must be True currently)
                bias=True,
                init_bias=0,
                reduction_rank=1, # (must be 1 currently)
                transpose=False,  # (must be False currently)
                max_temp_mem_size_in_samples=0):
    #UntestedBranchError("Convolution")
    if reduction_rank != 1:
        NotImplementedError("Convolution: reduction_rank other than 1 currently not supported")
    if transpose:
        NotImplementedError("Convolution: transpose option currently not supported")
    if not sharing:
        NotImplementedError("Convolution: sharing option currently must be True")
    output_channels_shape = _as_tuple(num_filters)
    output_rank = len(output_channels_shape)
    filter_rank = len(filter_shape)
    kernel_shape = _INFERRED * reduction_rank + filter_shape # kernel := filter plus reductionDims

    # parameters bound to this Function
    #init_kernel = glorot_uniform(filter_rank=-filter_rank, output_rank=1)
    init_kernel = _init_with(init, Record(filter_rank=filter_rank, output_rank=-1))
    # BUGBUG: It is very confusing that output_rank is negative, esp. since that means count from the start. Solution: add a flag
    d = init_kernel.__dict__
    W = Parameter(output_channels_shape + kernel_shape, init=init_kernel)                             # (K, C, H, W) aka [ W x H x C x K ]
    b = Parameter(output_channels_shape + (1,) * len(filter_shape), init=init_bias) if bias else None # (K,    1, 1) aka [ 1 x 1 x     K ]

    # expression
    x = Placeholder(name='convolution_arg')
    # TODO: can we update the parameter order of convolution to match the optional ones as in here? (options order matches Keras)
    # note: map_dims=num_filters not specified in Python (but in BS)
    apply_x = convolution (W, x,
                           strides=_as_tuple(strides),
                           sharing=_as_tuple(sharing),
                           auto_padding=_as_tuple(pad), #lower_pad=0, upper_pad=0,
                           # TODO: can we rename auto_padding to pad?
                           transpose=transpose,
                           max_temp_mem_size_in_samples=max_temp_mem_size_in_samples)
    if bias:
        apply_x = apply_x + b
    apply_x = apply_x >> activation
    return Block(apply_x, 'Convolution', Record(W=W, b=b))

# MaxPooling, AveragePooling -- create a max- or average-pooling layer
# TODO: do we need MaxPooling and AveragePooling?
# TODO: This is not really a layer as it does not hold learnable parameters. So:
#  - keep it in layer format, since users may think about it this way?
#  - turn it into a function (lower-case)? Then how would it work inside Sequential() (we'd need partial application)?
PoolingOp = Record(MAX='max', AVERAGE='average')  # create a const dictionary, acting like an enum  --TODO: what's the correct way?
def Pooling(op,      # PoolingOp.max or .average
            filter_shape,  # e.g. (3,3)
            pad=False,
            #lowerPad=None, upperPad=None, # TODO: clean this up; leaving it out for now
            strides=1):
    UntestedBranchError("Pooling")
    x = Placeholder(name='convolution_arg')
    apply_x = pooling (x, op, filter_shape, strides = strides, autoPadding = pad, lowerPad = lowerPad, upperPad = upperPad)
    #_name_and_extend_Function(apply_x, op + 'Pooling')
    return Block(apply_x, op + 'Pooling')

def MaxPooling(poolKind,      # PoolingKind.max or .average
               filter_shape,  # e.g. (3,3)
               pad=False,
               #lowerPad=None, upperPad=None, # TODO: clean this up; leaving it out for now
               strides=1):
    return Pooling(PoolingOp.MAX, filter_shape, pad=pad, strides=strides)

def AveragePooling(poolKind,      # PoolingKind.max or .average
                   filter_shape,  # e.g. (3,3)
                   pad=False,
                   #lowerPad=None, upperPad=None, # TODO: clean this up; leaving it out for now
                   strides=1):
    return Pooling(PoolingOp.AVERAGE, filter_shape, pad=pad, strides=strides)

# Recurrence() -- run a block recurrently over a time sequence
def Recurrence(over, _inf=None, go_backwards=False, initial_state=None):
    # helper to compute previous value
    # can take a single Variable/Function or a tuple
    if go_backwards:
        UntestedBranchError("Recurrence, go_backwards option")
    def previous_hook(state):
        if hasattr(state, 'outputs'):
           outputs = state.outputs
           if len(outputs) > 1:  # if multiple then apply to each element
               return tuple([previous_hook(s) for s in outputs])
        # not a tuple: must be a 'scalar', i.e. a single element
        return past_value  (state, initial_state) if not go_backwards else \
               future_value(state, initial_state)
    x = Placeholder(_inf=_inf, name='recurrence_arg')
    #x = Placeholder(name='recurrence_arg') # BUGBUG: Fails with "Variable with unknown dynamic axes detected when compiling the Function graph!"
    prev_state_forward = over.create_placeholder() # create a placeholder or a tuple of placeholders
    f_x_h_c = over(x, prev_state_forward) # apply the recurrent over
    # this returns a Function (x, (h_prev, c_prev)) -> (h, c)
    h = f_x_h_c.outputs[0]  # 'h' is a Variable (the output of a Function that computed it)
    if _trace_layers:
        _log_node(h)
        _log_node(combine([h.owner]))
    prev_state = previous_hook(f_x_h_c)  # delay (h, c)
    replacements = { value_forward: value.output for (value_forward, value) in zip(list(prev_state_forward), list(prev_state)) }
    f_x_h_c.replace_placeholders(replacements)  # binds _h_c := prev_state
    apply_x = combine([h.owner])     # the Function that yielded 'h', so we get to know its inputs
    # apply_x is a Function x -> h
    #_name_and_extend_Function(apply_x, 'Recurrence')
    if _trace_layers:
        _log_node(apply_x)
    return Block(apply_x, 'Recurrence', Record(over=over))

# Delay -- delay input
# TODO: This does not really have bound parameters. Should it still be a layer?
def Delay(T=1, initial_state=None):
    UntestedBranchError("Delay")

    # expression
    x = Placeholder(name='delay_arg')
    if T > 0:
        apply_x = past_value  (x, time_step=T, initial_state=initial_state)
    elif T < 0:
        apply_x = future_value(x, time_step=T, initial_state=initial_state)
    else:
        apply_x = x
    #_name_and_extend_Function(apply_x, 'Delay')
    return Block(apply_x, 'Delay', Record(T=T))

# Dropout -- create a drop-out layer
# Per-node dropout probabilities not yet supported, so one could also just use dropout directly.
def Dropout(prob=None):
    UntestedBranchError("Dropout")
    if prob is not None:
        raise NotImplementedError("Dropout: Dropout probability can currently not be specified per-layer.")
    apply_x = dropout
    #_name_and_extend_Function(apply_x, 'Dropout')
    return Block(apply_x, 'Dropout')

# BatchNormalization -- create a batch-normalization layer
def BatchNormalization(_inf, spatial_rank=0,  # reduce over these dims. E.g. 2 to reduce over (h,w) in a (C, H, W)-shaped input
                       init_scale=1,
                       normalization_time_constant=5000, blend_time_constant=0,
                       epsilon=0.00001, use_cntk_engine=True):
    #UntestedBranchError("BatchNormalization")
    # Note: This has been tested ad-hoc in SLUHandsOn.py, and gives quite precisely the expected improvement. So this works. Just need to fix _inf.

    # parameters bound to this Function
    norm_shape  = _INFERRED + (1,) * spatial_rank
    norm_shape  = _inf   # BUGBUG: remove once inference works
    if spatial_rank != 0:
        UntestedBranchError("BatchNormalization spatial_rank != 0:")
    scale       = Parameter(norm_shape, init=constant_initializer(init_scale))
    bias        = Parameter(norm_shape, init=constant_initializer(0))
    # BUGBUG: We need a parameter that is not updated, but is not a constant either
    # BUGBUG: the following fails: "ValueError: setting an array element with a sequence."
    #run_mean     = Constant(constant_initializer(0), shape=norm_shape)  # note: disable learning since these are updated differently
    #run_variance = Constant(constant_initializer(0), shape=norm_shape)
    import numpy as np
    init_stat = np.zeros(_inf, dtype=np.float32)
    run_mean     = Constant(init_stat)  # BUGBUG: replace by above once inference works
    run_variance = Constant(init_stat)

    # expression
    x = Placeholder(name='batch_normalization_arg')
    apply_x = batch_normalization(x, scale, bias, run_mean, run_variance, spatial_rank > 0, normalization_time_constant=normalization_time_constant, blend_time_constant=blend_time_constant, epsilon=epsilon,
                                  #use_cntk_engine=use_cntk_engine)
                                  use_cudnn_engine=not use_cntk_engine)
    #_name_and_extend_Function(apply_x, 'BatchNormalization')
    return Block(apply_x, 'BatchNormalization', Record(scale=scale, bias=bias, mean=run_mean, variance=run_variance))

# LayerNormalization -- create a layer-normalization layer
def LayerNormalization(initial_scale=1, initial_bias=0):
    UntestedBranchError("LayerNormalization")

    # parameters bound to this Function
    scale = Parameter((1), init=initial_scale)  # TODO: offer Softplus version for protection, as for Stabilizer
    bias = Parameter((1), init=initial_bias)

    # expression
    x = Placeholder(name='layer_normalization_arg')
    mean = reduce_mean (x) # normalize w.r.t. actual sample statistics
    x0 = x - mean;
    std = sqrt (reduce_mean (x0 * x0))
    x_hat = element_divide (x0, std)
    apply_x = x_hat * scale + bias    # denormalize with learned parameters
    #_name_and_extend_Function(apply_x, 'LayerNormalization')
    return Block(apply_x, 'LayerNormalization', Record(scale=scale, bias=bias))
