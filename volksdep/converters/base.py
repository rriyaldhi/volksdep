# adapted from torch2trt

import re

import torch
import torch.nn as nn
import tensorrt as trt

from .. import utils


__all__ = ['TRTModel', 'load', 'save']


def torch_dtype_from_trt(dtype):
    if dtype == trt.int8:
        return torch.int8
    elif trt.__version__ >= '7.0' and dtype == trt.bool:
        return torch.bool
    elif dtype == trt.int32:
        return torch.int32
    elif dtype == trt.float16:
        return torch.float16
    elif dtype == trt.float32:
        return torch.float32
    else:
        raise TypeError('{} is not supported by torch'.format(dtype))


def torch_device_from_trt(device):
    if device == trt.TensorLocation.DEVICE:
        return torch.device('cuda')
    elif device == trt.TensorLocation.HOST:
        return torch.device('cpu')
    else:
        raise TypeError('{} is not supported by torch'.format(device))


class TRTModel(nn.Module):
    def __init__(self, engine):
        """build TensorRT model with given engine

        Args:
            engine (trt.tensorrt.ICudaEngine)
        """

        super(TRTModel, self).__init__()

        self.engine = engine
        self.context = self.engine.create_execution_context()

        # get engine input tensor names and output tensor names
        self.input_names, self.output_names = [], []
        for idx in range(self.engine.num_bindings):
            name = self.engine.get_binding_name(idx)
            if not re.match(r'.* \[profile \d+\]', name):
                if self.engine.binding_is_input(idx):
                    self.input_names.append(name)
                else:
                    self.output_names.append(name)

        # default profile index is 0
        self.profile_index = 0

    @staticmethod
    def _rename(idx, name):
        if idx > 0:
            name += ' [profile {}]'.format(idx)

        return name

    def _activate_profile(self, inputs):
        for idx in range(self.engine.num_optimization_profiles):
            is_matched = True
            for name, inp in zip(self.input_names, inputs):
                name = self._rename(idx, name)
                min_shape, _, max_shape = self.engine.get_profile_shape(
                    idx, name)
                for s, min_s, max_s in zip(inp.shape, min_shape, max_shape):
                    is_matched = min_s <= s <= max_s

            if is_matched:
                if self.profile_index != idx:
                    self.profile_index = idx
                    self.context.active_optimization_profile = idx

                return True

        return False

    def _set_binding_shape(self, inputs):
        for name, inp in zip(self.input_names, inputs):
            name = self._rename(self.profile_index, name)
            idx = self.engine.get_binding_index(name)
            binding_shape = tuple(self.context.get_binding_shape(idx))
            shape = tuple(inp.shape)
            if shape != binding_shape:
                self.context.set_binding_shape(idx, shape)

    def _get_bindings(self, inputs):
        bindings = [None] * self.total_length
        outputs = [None] * self.output_length

        for i, name in enumerate(self.input_names):
            name = self._rename(self.profile_index, name)
            idx = self.engine.get_binding_index(name)
            dtype = torch_dtype_from_trt(self.engine.get_binding_dtype(idx))
            bindings[idx % self.total_length] = (
                inputs[i].to(dtype).contiguous().data_ptr())

        for i, name in enumerate(self.output_names):
            name = self._rename(self.profile_index, name)
            idx = self.engine.get_binding_index(name)
            print('idx: ' + str(idx))
            print(self.context)
            shape = tuple(self.context.get_binding_shape(idx))
            dtype = torch_dtype_from_trt(self.engine.get_binding_dtype(idx))
            device = torch_device_from_trt(self.engine.get_location(idx))
            output = torch.empty(
                size=shape, dtype=dtype, device=device).contiguous()
            outputs[i] = output
            bindings[idx % self.total_length] = output.data_ptr()

        return outputs, bindings

    @property
    def input_length(self):
        return len(self.input_names)

    @property
    def output_length(self):
        return len(self.output_names)

    @property
    def total_length(self):
        return self.input_length + self.output_length

    def forward(self, inputs):
        """run inference with inputs

        Args:
            inputs (torch.Tensor, tuple or list): inputs into trt engine.

        Returns:
            outputs (torch.Tensor or list): return torch.Tensor if there is
                only one output, else return list
        """

        inputs = utils.flatten(inputs)

        # support dynamic shape when engine has explicit batch dimension.
        if not self.engine.has_implicit_batch_dimension:
            status = self._activate_profile(inputs)
            assert status, (
                f'input shapes {[inp.shape for inp in inputs]} out of range')
            self._set_binding_shape(inputs)

        outputs, bindings = self._get_bindings(inputs)
        self.context.execute_async_v2(bindings,
                                      torch.cuda.current_stream().cuda_stream)

        if len(outputs) == 1:
            outputs = outputs[0]

        return outputs


def load(engine, log_level='ERROR'):
    """build trt engine from saved engine
    Args:
        engine (string): engine file name to load
        log_level (string, default is ERROR): tensorrt logger level,
            INTERNAL_ERROR, ERROR, WARNING, INFO, VERBOSE are support.
    """

    logger = trt.Logger(getattr(trt.Logger, log_level))

    with open(engine, 'rb') as f, trt.Runtime(logger) as runtime:
        engine = runtime.deserialize_cuda_engine(f.read())

    trt_model = TRTModel(engine)

    return trt_model


def save(model, name):
    """
        model (volksdep.converters.base.TRTModel)
        name (string): saved file name
    """

    engine = model.engine

    with open(name, 'wb') as f:
        f.write(engine.serialize())
