from typing import Dict, Iterable, Iterator, Union, List

from .utils import round_up
from .global_var import config
import torch
from . import nccl
from .synchronize import wait_loader
from .parameter import DistributedParameter, OpAllGather
from .checkpointing import ScopedTensorInspectorContext
from . import debug
import copy
import inspect
def divide_input(args):
    tensors = []
    others = []
    for arg in args:
        if torch.is_tensor(arg):
            tensors.append(arg)
            others.append(None)
        else:
            tensors.append(None)
            others.append(arg)
    return tensors,others

def combine_input(tensors, others, detach=True):
    input_reqires_grad = []
    all_inputs = []
    for tensor, other in zip(tensors, others):
        if tensor is None:
            all_inputs.append(other)
            input_reqires_grad.append(False)
        else:
            if detach:
                input_reqires_grad.append( tensor.requires_grad )
                nw_tensor = tensor.detach()
                nw_tensor.requires_grad = tensor.requires_grad
                all_inputs.append(nw_tensor)
            else:
                input_reqires_grad.append( tensor.requires_grad )
                all_inputs.append(tensor)

    return all_inputs, input_reqires_grad

def kwargs_convert(len_args ,args):
    inp_args = args[:len_args]
    inp_kwargs = {}
    for k, v in zip(args[len_args::2], args[len_args + 1::2]):
        inp_kwargs[k] = v
    return inp_args,inp_kwargs



class CheckpointFunction(torch.autograd.Function):
    """This function is adapted from torch.utils.checkpoint with
       two main changes:
           1) torch.cuda.set_rng_state is replaced with `_set_cuda_rng_state`
           2) the states in the model parallel tracker are also properly
              tracked/set/reset.
    """
    @staticmethod
    def forward(ctx, inner_func, preserve_rng_state, len_args, *args):
        ctx.cuda_rng_state = torch.cuda.get_rng_state() if preserve_rng_state else None
        tensors,others = divide_input(args)
        ctx.len_args = len_args
        ctx.save_for_backward(tensors)
        ctx.inner_func = inner_func
        inp_args, inp_kwargs = kwargs_convert(len_args, args)
        
        with torch.no_grad():
            outputs = inner_func(*inp_args, **inp_kwargs)

        return outputs

    @staticmethod
    def backward(ctx, *args):
        all_inputs, input_reqires_grad = combine_input(ctx.saved_tensors, ctx.nontensor_inputs)
        with torch.random.fork_rng(devices=[torch.cuda.current_device()], enabled=ctx.preserve_rng_state):
            if ctx.preserve_rng_state:
                torch.cuda.set_rng_state(ctx.cuda_rng_state)
            with torch.enable_grad():
                inp_args, inp_kwargs = divide_input(ctx.len_args, args)
                outputs = ctx.block._module._call_impl(*inp_args, **inp_kwargs)
                if not isinstance(outputs, tuple):
                    outputs = (outputs,)
                outputs_with_grad = []
                grad_of_output = []
                for i, output in enumerate(outputs):
                    if torch.is_tensor(output) and output.requires_grad:
                        outputs_with_grad.append(output)
                        grad_of_output.append(grads[i])

                # calculate gradients for inputs, also for parameters
                torch.autograd.backward(
                    outputs_with_grad,
                    grad_of_output + list(grads[len(outputs):]),
                )
        grads = []
        for inp, requires_grad in zip(all_inputs, input_reqires_grad):
            if requires_grad:
                grads.append(inp.grad)
            else:
                grads.append(None)
        return (None, None, None) + tuple(grads)


def checkpoint(func):
    def checkpoint_func(*args):
        return CheckpointFunction.apply(func,*args)
    return checkpoint_func

class OpZeRO(torch.autograd.Function):
    @staticmethod
    def forward(ctx, placeholder, block : 'ZeROBlock', preserve_rng_state, len_args, *args):
        ctx.block = block
        ctx.checkpointing = block.checkpointing
        ctx.preserve_rng_state = preserve_rng_state
        tensors = []
        others = []
        tensors,others = divide_input(args)
        ctx.nontensor_inputs = others
        ctx.len_args = len_args
        ctx.save_for_backward(*tensors)
        ctx.param_dict={}
        if config['zero_level'] == 2:
            flag = 1
        else:
            flag = 0
        with ScopedTensorInspectorContext() as inspector, ZeROContext(block, ctx.param_dict, flag):
            if ctx.checkpointing:
                call_func = checkpoint(ctx.block._module._call_impl)
                outputs = call_func(preserve_rng_state, len_args, *args)
            else:
                inp_args,inp_kwargs = kwargs_convert(len_args, args)
                outputs = ctx.block._module._call_impl(*inp_args, **inp_kwargs)
        for it in inspector.hidden_states:
            debug.append("_inspect_hidden_states", it)
        ctx.inspect_list = inspector.hidden_states

        if not isinstance(outputs, list) and not isinstance(outputs, tuple):
            outputs = [outputs]
            len_outputs = 0
        else:
            outputs = list(outputs)
            len_outputs = len(outputs)
        ctx.outputs = outputs
        return tuple([len_outputs] + outputs + [hidden_state["tensor"] for hidden_state in inspector.hidden_states])

    @staticmethod
    def backward(ctx, _, *grads):
        if not torch.autograd._is_checkpoint_valid():
            raise RuntimeError(
                "Checkpointing is not compatible with .grad() or when an `inputs` parameter"
                " is passed to .backward(). Please use .backward() and do not pass its `inputs`"
                " argument.")

        all_inputs = []
        input_reqires_grad = []
        len_args = ctx.len_args
        for tensor, other in zip(ctx.saved_tensors, ctx.nontensor_inputs):
            if tensor is None:
                all_inputs.append(other)
                input_reqires_grad.append(False)
            else:
                input_reqires_grad.append( tensor.requires_grad )
                nw_tensor = tensor.detach()
                nw_tensor.requires_grad = tensor.requires_grad
                all_inputs.append(tensor)
        
            if config['zero_level'] == 2:
                flag = 2
            else:
                flag = 0
        outputs = ctx.outputs
        with ZeROContext(ctx.block, ctx.param_dict, flag):
            if not isinstance(outputs, tuple):
                outputs = (outputs,)

            assert len(outputs) + len(inspector.hidden_states) == len(grads)

            outputs_with_grad = []
            grad_of_output = []
            for i, output in enumerate(outputs):
                if torch.is_tensor(output) and output.requires_grad:
                    outputs_with_grad.append(output)
                    grad_of_output.append(grads[i])

            # calculate gradients for inputs, also for parameters
            torch.autograd.backward(
                outputs_with_grad + [hidden_state["tensor"] for hidden_state in inspector.hidden_states],
                grad_of_output + list(grads[len(outputs):]),
            )
        assert len(ctx.inspect_list) == len(inspector.hidden_states), "Backward step changed"
        for i, it in enumerate(inspector.hidden_states):
            assert it["name"] == ctx.inspect_list[i]["name"], "Backward step changed"
            assert it["shape"] == ctx.inspect_list[i]["shape"], "Backward step changed"
            assert it["group"] == ctx.inspect_list[i]["group"], "Backward step changed"
            
            # change the tensor in placeholder
            ctx.inspect_list[i]["tensor"] = it["tensor"]
            ctx.inspect_list[i]["requires_grad"] = it["requires_grad"]

        grads = []
        for inp, requires_grad in zip(all_inputs, input_reqires_grad):
            if requires_grad:
                grads.append(inp.grad)
            else:
                grads.append(None)
        return (None, None, None, None) + tuple(grads)

class ZeROContext:
    def __init__(self, block : 'ZeROBlock', ctx_dict : dict = {}, forward = True, pipe = False, save=False) -> None:
        self.block = block
        self.ctx_dict = ctx_dict
        self._param_buffer = {}
        self._grad_buffer = {}
        self._param_tensor = {}
        self._grad_tensor = {}
        self.forward = forward
        if not save:
            if forward and config["zero_level"] == 2:
                self.flag = 1
            elif config["zero_level"] == 2:
                self.flag = 2
            else:
                self.flag = 0
        else:
            self.flag = 0
            
        self._need_release = False
        if pipe:
            self.comm = config["zero_comm"] 
        else:
            self.comm = config["comm"]
    def enter(self):
        """
        gather parameters
        """
        if self.block._ready:
            return
        self.block._ready = True
        self._need_release = True

        wait_loader()
        requires_grad = torch.is_grad_enabled()
        with torch.cuda.stream(config["load_stream"]):
            for kw, val in self.block._storage_info.items():
                assert self.block._storage_params[kw].is_cuda
                assert kw not in self._grad_buffer
                assert kw not in self._param_buffer
                local_param = self.block._storage_params[kw]
           
                storage_type = local_param.storage_type()
                if self.flag != 2:
                    self._param_buffer[kw] = storage_type(val["partition_size"] * val["world_size"])
                    self._param_tensor[kw] = torch.tensor([], dtype=self._param_buffer[kw].dtype, device=self._param_buffer[kw].device).set_(self._param_buffer[kw])
                if not self.forward and local_param.requires_grad:
                    self._grad_buffer[kw] = storage_type(val["partition_size"] * val["world_size"])
                    self._grad_tensor[kw] = torch.tensor([], dtype=self._grad_buffer[kw].dtype, device=self._grad_buffer[kw].device).set_(self._grad_buffer[kw]).zero_()
            if self.flag != 2:
                nccl.groupStart()
                for kw, val in self.block._storage_info.items():
                    nccl.allGather(
                        self.block._storage_params[kw].storage(),
                        self._param_buffer[kw],
                        self.comm
                    )
                nccl.groupEnd()

        current_stream = torch.cuda.current_stream()
        current_stream.wait_stream(config["load_stream"])
        
        # set wait stream for each storage
        for kw in self.block._storage_info.keys():
            if self.flag != 2:
                self._param_tensor[kw].record_stream(current_stream)
            if not self.forward and kw in self._grad_tensor:
                self._grad_tensor[kw].record_stream(current_stream)

        # update parameters in block
        for param in self.block._param_info:
            kw_name = param["kw_name"]
            offset = param["offset"]
            shape = param["shape"]

            if self.flag != 2:
                dtype = self._param_buffer[kw_name].dtype
                device = self._param_buffer[kw_name].device
                param["parameter"].data = torch.tensor([], dtype=dtype, device=device).set_(self._param_buffer[kw_name], offset, shape)                
            else:
                dtype = param["parameter"].data.dtype
                device = param["parameter"].data.device
                param["parameter"].data = torch.tensor([], dtype=dtype, device=device).set_(self.ctx_dict[kw_name], offset, shape)

            if not self.forward and kw_name in self._grad_buffer and param["parameter"].requires_grad:
                param["parameter"].grad = torch.tensor([], dtype=dtype, device=device).set_(self._grad_buffer[kw_name], offset, shape)

    def __enter__(self):
        self.enter()
    
    def exit(self):
        """
        Reduce scatter gradients
        """

        if not self._need_release:
            return
        self._need_release = False
        self.block._ready = False
        requires_grad = torch.is_grad_enabled()
        if not self.forward:
            for kw, val in self.block._storage_info.items():
                local_param = self.block._storage_params[kw]

                # accumulate previous gradient
                if local_param.requires_grad:
                    if local_param.grad is None:
                        grad_storage = val["storage_type"](val["partition_size"])   # initialize gradient if not exist
                        local_param.grad = torch.tensor([], dtype=grad_storage.dtype, device=grad_storage.device).set_(grad_storage).zero_()
                    else:
                        self._grad_tensor[kw][val["begin"]:val["end"]] += local_param.grad
            
            current_stream = torch.cuda.current_stream()
            config["load_stream"].wait_stream(current_stream)   # wait for backward

            with torch.cuda.stream(config["load_stream"]):
                nccl.groupStart()
                for kw, val in self.block._storage_info.items():
                    local_param = self.block._storage_params[kw]

                    # scatter gradient
                    if local_param.requires_grad:
                        nccl.reduceScatter(
                            self._grad_buffer[kw],
                            local_param.grad.storage(),
                            "sum",
                            self.comm
                        )
                nccl.groupEnd()

            # set wait stream for each storage
            for kw in self._grad_tensor.keys():
                # grads can not be freed until reduce ops finish
                self._grad_tensor[kw].record_stream(config["load_stream"])

        # Release all parameters from buffer to block_storge
        for param in self.block._param_info:
            kw_name = param["kw_name"]
            dtype = self.block._storage_params[kw_name].dtype
            device = self.block._storage_params[kw_name].device
            if "begin" not in param:
                param["parameter"].data = torch.tensor([], dtype=dtype, device=device)
                param["parameter"].grad = None
                continue
            begin = param["begin"]
            end = param["end"]
            param["parameter"].data = torch.tensor([], dtype=dtype, device=device).set_(self.block._storage_params[kw_name].storage(), begin, end)
            if param["parameter"].requires_grad and self.block._storage_params[kw_name].grad is not None:
                param["parameter"].grad = torch.tensor([], dtype=dtype, device=device).set_(self.block._storage_params[kw_name].grad.storage(), begin, end)
        if self.flag == 1:
            for i in self._param_buffer:
                self.ctx_dict[i] = self._param_buffer[i]
        self._grad_tensor = {}
        self._param_tensor = {}
        self._grad_buffer = {}
        self._param_buffer = {}
    def __exit__(self, exc_type, exc_val, exc_tb):
        # reduce scatter gradients
        self.exit()

def storage_type_cuda(storage_type):
    STORAGE_MAP = {
        torch.FloatStorage: torch.cuda.FloatStorage,
        torch.DoubleStorage: torch.cuda.DoubleStorage,
        torch.HalfStorage: torch.cuda.HalfStorage,
        torch.BFloat16Storage: torch.cuda.BFloat16Storage,
        torch.CharStorage: torch.cuda.CharStorage,
        torch.ByteStorage: torch.cuda.ByteStorage,
        torch.ShortStorage: torch.cuda.ShortStorage,
        torch.IntStorage: torch.cuda.IntStorage,
        torch.cuda.FloatStorage: torch.cuda.FloatStorage,
        torch.cuda.DoubleStorage: torch.cuda.DoubleStorage,
        torch.cuda.HalfStorage: torch.cuda.HalfStorage,
        torch.cuda.BFloat16Storage: torch.cuda.BFloat16Storage,
        torch.cuda.CharStorage: torch.cuda.CharStorage,
        torch.cuda.ByteStorage: torch.cuda.ByteStorage,
        torch.cuda.ShortStorage: torch.cuda.ShortStorage,
        torch.cuda.IntStorage: torch.cuda.IntStorage,
    }
    if storage_type not in STORAGE_MAP:
        raise ValueError("Unknown storage type: {}".format(storage_type))
    return STORAGE_MAP[storage_type]

def _get_param_kw(param : DistributedParameter):
    type_name = str(param.dtype).split(".")[-1]
    grad_name = "_grad" if param.requires_grad else "_nograd"
    group_name = ""
    if param.group is not None:
        group_name = "_g_" + param.group
    return type_name + grad_name + group_name

class ZeROBlock(torch.nn.Module):
    """ Checkpoint a model or part of the model.

    Checkpoint block is used to save the occupation of GPU memory in training.

    For details, please refer to `Checkpointing <https://pytorch.org/docs/stable/checkpoint.html>`_ .

    Args:
        model (torch.nn.Module): The model to be checkpointed. All kinds of modules are supported.
    
    Examples:
        >>> transformer_block = TransformerBlock(...)
        >>> checkpoint_block = ZeROBlock(transformer_block)
        >>> y1, ... = checkpoint_block(x)
        >>> y2, ... = transformer_block(x)
        >>> assert torch.allclose(y1, y2)
    """
    def __init__(self, inner_module : torch.nn.Module):
        super().__init__()
        self._module = inner_module
        self.checkpointing = config["checkpointing"]
        # build large parameter&grad here
        self._param_info = []
        self._storage_params : Dict[str, torch.nn.Parameter] = {}
        self._storage_info = {}
        self._ready = False
        # sort parameters by name
        ordered_parameters = list(self._module.named_parameters())

        # calc total number of parameters
        for name, param in ordered_parameters:
            if not isinstance(param, DistributedParameter):
                raise ValueError("All parameters in checkpoint block must be DistributedParameter.")

            storage_type = storage_type_cuda(param.storage_type())
            kw_name = _get_param_kw(param)

            if kw_name not in self._storage_info:
                self._storage_info[kw_name] = {
                    "total": 0,
                    "storage_type": storage_type,
                    "requires_grad": param.requires_grad,
                    "group": param.group
                }

            param_shape = param._original_shape

            self._storage_info[kw_name]["total"] = round_up(
                self._storage_info[kw_name]["total"] + param_shape.numel(), 
                512 // param.element_size()
                # 512 bytes aligned
            )

        offsets = {}
        # intialize storage buffers
        for kw, val in self._storage_info.items():
            val["world_size"] = config["world_size"]
            partition_size = round_up(val["total"], val["world_size"]) // val["world_size"]
            val["partition_size"] = partition_size
            val["begin"] = config['rank'] * partition_size
            val["end"] = (config['rank'] + 1) * partition_size
            offsets[kw] = 0


            storage_type = val["storage_type"]

            storage_param_buffer = storage_type(partition_size)

            dtype = storage_param_buffer.dtype
            device = storage_param_buffer.device

            # bind storage to buffer tensor
            storage_param = torch.nn.Parameter(
                torch.tensor([], dtype=dtype, device=device).set_(storage_param_buffer)
            )
            if val["requires_grad"]:
                storage_param.requires_grad_(True)
            else:
                storage_param.requires_grad_(False)

        
            self._storage_params[kw] = storage_param

        # initialize parameters in module
        for name, param in ordered_parameters:
            param_shape = param._original_shape
            kw_name = _get_param_kw(param)

            param_st = offsets[kw_name]
            offsets[kw_name] += param_shape.numel()
            param_end = offsets[kw_name]
            offsets[kw_name] = round_up(offsets[kw_name], 512 // param.element_size())

            self._param_info.append({
                "parameter": param,
                "name": name,
                "offset": param_st,
                "size": param_shape.numel(),
                "shape": param_shape,
                "kw_name": kw_name,
            })

            # copy values to buffer for normal parameter
            storage_st = self._storage_info[kw_name]["begin"]
            storage_end = self._storage_info[kw_name]["end"]
            
            # make parameter contiguous in storage
            with torch.no_grad():
                contiguous_param = OpAllGather.apply(param)

            if not (param_st >= storage_end or param_end <= storage_st):
                # copy offset in parameter storage
                offset_st = max(storage_st - param_st, 0)
                offset_end = min(storage_end - param_st, contiguous_param.numel())
                assert offset_st < offset_end

                # copy to offset in buffer storage
                to_offset_st = offset_st + param_st - storage_st
                to_offset_end = offset_end + param_st - storage_st
                
                # copy to buffer
                # PyTorch 1.11 changed the API of storage.__getitem__
                d_dtype = self._storage_params[kw_name].dtype
                d_device = self._storage_params[kw_name].device
                param.data = torch.tensor([], dtype=param.dtype, device=param.device).set_(self._storage_params[kw_name].storage(), to_offset_st, (to_offset_end - to_offset_st,))
                self._param_info[-1]["begin"] = to_offset_st
                self._param_info[-1]["end"] = (to_offset_end - to_offset_st,)
                param.data[:] = \
                    torch.tensor([], dtype=d_dtype, device=d_device).set_(contiguous_param.storage(), offset_st, (offset_end - offset_st,))[:]
                del contiguous_param
            else:
                param.data = torch.tensor([], dtype=param.dtype, device=param.device)

            # clear parameter data, but keep the dtype and device
            setattr(param, "_in_checkpoint_block", True)

        for kw in offsets.keys():
            assert offsets[kw] == self._storage_info[kw]["total"]
    
    def __call__(self, *args, **kwargs):
        # gather here
        placeholder = torch.tensor([], requires_grad=torch.is_grad_enabled())
        all_inputs = list(args)
        for kw, val in kwargs.items():
            all_inputs.append(kw)
            all_inputs.append(val)
        outputs = OpZeRO.apply(placeholder, self, True, len(args), *all_inputs)
        len_output = outputs[0]
        return outputs[1:1+len_output] if len_output > 0 else outputs[1]

    def __getattr__(self,name:str):
        if name=="_module":
            return self._module
        return getattr(self._module, name)

    def __setattr__(self, name, value):
        object.__setattr__(self, name, value)

    def __getattribute__(self, name: str):
        if name=="_parameters":
            return self._module._parameters
        return super().__getattribute__(name)

    def __delattr__(self, name):
        object.__delattr__(self, name)

    def _save_to_state_dict(self, destination, prefix, keep_vars):
        raise RuntimeError("._save_to_state_dict() of ZeROBlock should not be called")
    
    def state_dict(self, destination=None, prefix='', keep_vars=False):
        # gather here
        with torch.no_grad():
            with ZeROContext(self, save=True):
                return self._module.state_dict(destination=destination, prefix=prefix, keep_vars=keep_vars)
    
    def _load_from_state_dict(self, state_dict, prefix, local_metadata, strict,
                              missing_keys, unexpected_keys, error_msgs):
        all_keys = []
        for it in self._param_info:
            key = prefix + it["name"]
            all_keys.append(key)
            if key in state_dict:
                # load here
                input_param = state_dict[key]
                if input_param.shape != it["shape"]:
                    error_msgs.append('size mismatch for {}: copying a param with shape {} from checkpoint, '
                                      'the shape in current model is {}.'
                                      .format(key, input_param.shape, it["shape"]))
                    continue
                param_st = it["offset"]
                param_end = it["offset"] + it["size"]
                kw_name = it["kw_name"]

                # not in this partition
                storage_st = self._storage_info[kw_name]["begin"]
                storage_end = self._storage_info[kw_name]["end"]
                if param_st >= storage_end:
                    continue
                if param_end <= storage_st:
                    continue
                    
                # copy to buffer
                assert input_param.numel() == it["size"]
                contiguous_param = input_param.to(it["parameter"].dtype).cuda().contiguous()
                
                offset_st = max(storage_st - param_st, 0)
                offset_end = min(storage_end - param_st, contiguous_param.numel())
                assert offset_st < offset_end

                to_offset_st = offset_st + param_st - storage_st
                to_offset_end = offset_end + param_st - storage_st
                
                # copy to buffer
                # PyTorch 1.11 changed the API of storage.__getitem__
                d_dtype = self._storage_params[kw_name].dtype
                d_device = self._storage_params[kw_name].device
                torch.tensor([], dtype=d_dtype, device=d_device).set_(self._storage_params[kw_name].storage(), to_offset_st, (to_offset_end - to_offset_st,))[:] = \
                    torch.tensor([], dtype=d_dtype, device=d_device).set_(contiguous_param.storage(), offset_st, (offset_end - offset_st,))[:]
                del contiguous_param
            elif strict:
                missing_keys.append(key)

        for name, param in self.named_parameters():
            if isinstance(param, DistributedParameter) and not param._in_checkpoint_block:
                key = prefix + name
                all_keys.append(key)
                if key in state_dict:
                    input_param = state_dict[key]
                    is_param_lazy = torch.nn.parameter.is_lazy(param)
                    # Backward compatibility: loading 1-dim tensor from 0.3.* to version 0.4+
                    if not is_param_lazy and len(param.shape) == 0 and len(input_param.shape) == 1:
                        input_param = input_param[0]

                    if not is_param_lazy and not isinstance(param, DistributedParameter) and input_param.shape != param.shape:
                        # local shape should match the one in checkpoint
                        error_msgs.append('size mismatch for {}: copying a param with shape {} from checkpoint, '
                                        'the shape in current model is {}.'
                                        .format(key, input_param.shape, param.shape))
                        continue
                    if not is_param_lazy and isinstance(param, DistributedParameter) and input_param.shape != param._original_shape:
                        error_msgs.append('size mismatch for {}: copying a param with shape {} from checkpoint, '
                                        'the shape in current model is {}.'
                                        .format(key, input_param.shape, param.shape))
                    try:
                        with torch.no_grad():
                            param._copy_data(input_param)
                    except Exception as ex:
                        error_msgs.append('While copying the parameter named "{}", '
                                        'whose dimensions in the model are {} and '
                                        'whose dimensions in the checkpoint are {}, '
                                        'an exception occurred : {}.'
                                        .format(key, param.size(), input_param.size(), ex.args))
                elif strict:
                    missing_keys.append(key)

        if strict:
            all_keys = set(all_keys)
            for key in state_dict.keys():
                if key.startswith(prefix) and key not in all_keys:
                    unexpected_keys.append(key)
        
    def grouped_parameters(self):
        ret = {}
        for kw, val in self._storage_info.items():
            if val["group"] not in ret:
                ret[val["group"]] = []
            ret[val["group"]].append(self._storage_params[kw])
        for kw, val in ret.items():
            yield kw, val

    def init_parameters(self):
        """
        Initialize distributed parameters in this block.
        """
        for it in self._param_info:
            param = it["parameter"]
            if isinstance(param, DistributedParameter) and param._init_method is not None:
                # initialzie here
                tmp_tensor = torch.empty(it["shape"], device=param.device, dtype=param.dtype)
                param._init_method(tmp_tensor)
                param_st = it["offset"]
                param_end = it["offset"] + it["size"]
                kw_name = it["kw_name"]

                # not in this partition
                storage_st = self._storage_info[kw_name]["begin"]
                storage_end = self._storage_info[kw_name]["end"]
                if param_st >= storage_end:
                    continue
                if param_end <= storage_st:
                    continue
                    
                # copy to buffer
                assert tmp_tensor.is_contiguous() and it["size"] == tmp_tensor.numel()
                
                offset_st = max(storage_st - param_st, 0)
                offset_end = min(storage_end - param_st, tmp_tensor.numel())
                assert offset_st < offset_end

                to_offset_st = offset_st + param_st - storage_st
                to_offset_end = offset_end + param_st - storage_st
                
                # copy to buffer
                # PyTorch 1.11 changed the API of storage.__getitem__
                d_dtype = self._storage_params[kw_name].dtype
                d_device = self._storage_params[kw_name].device
                param.data[:] = \
                    torch.tensor([], dtype=d_dtype, device=d_device).set_(tmp_tensor.storage(), offset_st, (offset_end - offset_st,))[:]
                del tmp_tensor
        
    def _named_members(self, get_members_fn, prefix='', recurse=True, **kwargs):
        r"""Helper method for yielding various names + members of modules."""
        
        #compitibity with torch 2.0
        if "remove_duplicate" in inspect.signature(torch.nn.Module._named_members).parameters and "remove_duplicate" not in kwargs:
            kwargs['remove_duplicate'] = True
        return self._module._named_members(get_members_fn, prefix, recurse, **kwargs)
    
    def named_modules(self, memo = None, prefix: str = '', remove_duplicate: bool = True):
        r"""Returns an iterator over all modules in the network, yielding
        both the name of the module as well as the module itself.

        Args:
            memo: a memo to store the set of modules already added to the result
            prefix: a prefix that will be added to the name of the module
            remove_duplicate: whether to remove the duplicated module instances in the result
            or not

        Yields:
            (string, Module): Tuple of name and module

        Note:
            Duplicate modules are returned only once. In the following
            example, ``l`` will be returned only once.

        Example::

            >>> l = nn.Linear(2, 2)
            >>> net = nn.Sequential(l, l)
            >>> for idx, m in enumerate(net.named_modules()):
                    print(idx, '->', m)

            0 -> ('', Sequential(
              (0): Linear(in_features=2, out_features=2, bias=True)
              (1): Linear(in_features=2, out_features=2, bias=True)
            ))
            1 -> ('0', Linear(in_features=2, out_features=2, bias=True))

        """

        if memo is None:
            memo = set()
        if self not in memo:
            if remove_duplicate:
                memo.add(self)
            yield prefix, self
            for name, module in self._module._modules.items():
                if module is None:
                    continue
                submodule_prefix = prefix + ('.' if prefix else '') + name
                for m in module.named_modules(memo, submodule_prefix, remove_duplicate):
                    yield m

    def named_children(self):
        return self._module.named_children()
    
    def train(self, mode: bool = True):
        self._module.train(mode)

    def eval(self):
        self._module.eval()
    
    def __repr__(self):
        return self._module.__repr__()
        
class OpTransformerBlockList(torch.autograd.Function):
    @staticmethod
    def forward(ctx, placeholder, self : 'TransformerBlockList', num_hidden, *args):
        tensors = []
        others = []
        ctx.checkpointing = self.checkpointing
        tensors, others = divide_input(args[num_hidden:])
        hidden_states = args[:num_hidden]
    
        ctx.nontensor_inputs = others
        ctx.self = self
        ctx.layers_dict = [{} for _ in range(len(self))]
        layer_inputs = []
        layer_inspector = []
        cuda_rng_state = []
        for i in range(len(self)):
            with torch.enable_grad():
                layer_inputs += [hidden_state for hidden_state in hidden_states]
                cuda_rng_state.append( torch.cuda.get_rng_state() )
                block_ctx = ZeROContext(self._modules[str(i)], ctx.layers_dict[i], forward=True)
                # gather parameter on load stream
                block_ctx.enter()
                if not ctx.checkpointing:
                    hidden_states = self._modules[str(i)]._module._call_impl(*hidden_states, *args[num_hidden:])
                else:
                    call_func = checkpoint(self._modules[str(i)]._module._call_impl)
                    hidden_states = call_func(True,num_hidden,*args)
                if not isinstance(hidden_states, tuple):
                    hidden_states = (hidden_states,)
                block_ctx.exit()
        layer_inputs += [hidden_state for hidden_state in hidden_states]
        ctx.layer_inspector = layer_inspector
        ctx.cuda_rng_state = cuda_rng_state
        ctx.num_hidden = num_hidden
        ctx.save_for_backward(*layer_inputs, *tensors)

        return tuple([hidden_state.clone() for hidden_state in hidden_states])


    @staticmethod
    def backward(ctx, *grads):
        grad_hidden_states = [g for g in grads[:ctx.num_hidden]]
        grad_middles = grads[ctx.num_hidden:2*ctx.num_hidden]
        grad_inspectors = grads[2*ctx.num_hidden:]
        def exit_prev(prev_ctx, prev_grad):
            if prev_ctx is not None:
                if prev_grad:
                    with torch.enable_grad():
                        prev_ctx.exit()
                        config["load_stream"].record_event(config["load_event"])
                else:
                    with torch.no_grad():
                        prev_ctx.exit()
                        config["load_stream"].record_event(config["load_event"])
                
        if not torch.autograd._is_checkpoint_valid():
            raise RuntimeError(
                "Checkpointing is not compatible with .grad() or when an `inputs` parameter"
                " is passed to .backward(). Please use .backward() and do not pass its `inputs`"
                " argument.")
        
        layer_inputs = ctx.saved_tensors[:(len(ctx.self)+1) * ctx.num_hidden]
        save_args = ctx.saved_tensors[(len(ctx.self)+1) * ctx.num_hidden:]
        all_inputs, input_requires_grad = combine_input(save_args,ctx.nontensor_inputs,detach=False)
        
        with torch.random.fork_rng(devices=[torch.cuda.current_device()], enabled=True):
                # overlap load and scatter here
            prev_ctx = None
            prev_grad = False
            for i in reversed(range(len(ctx.self))):
                torch.cuda.set_rng_state(ctx.cuda_rng_state[i])
                ipts = layer_inputs[(i)*ctx.num_hidden:(i+1)*ctx.num_hidden]
                block_ctx = ZeROContext(ctx.self._modules[str(i)], ctx.layers_dict[i], forward=False)
                block_ctx.enter()
                exit_prev(prev_ctx, prev_grad)
                prev_ctx = block_ctx
                prev_grad = True
                outputs = layer_inputs[(i+1)*ctx.num_hidden:(i+2)*ctx.num_hidden]
                # if len(inspector_hidden_states) > 0:
                #     torch.autograd.backward(
                #         list(outputs) + [hidden_state["tensor"] for hidden_state in inspector_hidden_states],
                #         grad_hidden_states + grad_inspectors[-len(inspector_hidden_states):],
                #         inputs = ipts,
                #     )
                #     grad_inspectors = grad_inspectors[:-len(inspector_hidden_states)]
                # else:
                torch.autograd.backward(
                    outputs,
                    grad_hidden_states,
                    inputs = ipts,
                )
                grad_hidden_states = tuple([ipt.grad for ipt in ipts])
            
            exit_prev(prev_ctx, prev_grad)
        grads = []
        for inp, requires_grad in zip(all_inputs, input_requires_grad):
            if requires_grad:
                grads.append(inp.grad)
            else:
                grads.append(None)
        return (None, None, None) + grad_hidden_states + tuple(grads)
    
class TransformerBlockList(torch.nn.Module):
    r"""
    TransformerBlockList is a list of ZeROBlocks.

    This is designed to reduce the communication overhead by overlapping the computation and reduce_scatter operation during backward pass.

    It is similar to `torch.nn.ModuleList` but with the difference when calling .forward() and .backward().

    Example:
        >>> module_list = [ ... ]
        >>> normal_module_list = torch.nn.ModuleList(module_list)
        >>> transformer_module_list = TransformerBlockList(module_list)
        >>> # Calling normal module list
        >>> for layer in normal_module_list:
        >>>     hidden_state = layer.forward(hidden_state, ...)
        >>> # Calling transformer module list
        >>> hidden_state = transformer_module_list(hidden_state, ...)

    """
    _modules: Dict[str, ZeROBlock]

    def __init__(self, modules: Iterable[ZeROBlock], num_hidden=1, sqrt=False) -> None:
        super().__init__()
        
        self._modules = {}
        self.checkpointing = config["checkpointing"]
        for i, module in enumerate(modules):
            if not isinstance(module, ZeROBlock):
                module = ZeROBlock(module)
            self._modules[str(i)] = module
            self.add_module(str(i), module)

        self.num_hidden = num_hidden

            
    def __len__(self) -> int:
        return len(self._modules)
    def __iter__(self) -> Iterator[ZeROBlock]:
        return iter(self._modules.values())
    def __getitem__(self, index: Union[int, str]) -> ZeROBlock:
        return self._modules[str(index)]

    def forward(self, *args, return_hidden_states = False):
        self.return_hidden_states = return_hidden_states
        placeholder = torch.tensor([], requires_grad=torch.is_grad_enabled())
        outputs = OpTransformerBlockList.apply(placeholder, self, self.num_hidden, *args)
        if return_hidden_states:
            return tuple(outputs[:2*self.num_hidden])
        else:
            return tuple(outputs[:self.num_hidden]) if self.num_hidden > 1 else outputs[0]