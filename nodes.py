import torch
import torch.nn.functional as F
from torchvision import transforms
from huggingface_hub import hf_hub_download
import os
import sys
import math

import comfy.model_management as mm
from comfy.utils import ProgressBar, load_torch_file
import folder_paths

script_directory = os.path.dirname(os.path.abspath(__file__))
sys.path.append(script_directory)

import models
from transport import ODE
from transformers import AutoModel, AutoTokenizer

class DownloadAndLoadLuminaModel:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
            "model": (
                    [ 
                    'Alpha-VLLM/Lumina-Next-SFT',
                    ],
                    {
                    "default": 'Alpha-VLLM/Lumina-Next-SFT'
                    }),
            "precision": ([ 'bf16','fp32'],
                    {
                    "default": 'bf16'
                    }),
            "hf_token": ("STRING", { "default": "" }),
            },
        }

    RETURN_TYPES = ("LUMINAMODEL",)
    RETURN_NAMES = ("lumina_model",)
    FUNCTION = "loadmodel"
    CATEGORY = "LuminaWrapper"

    def loadmodel(self, model, precision, hf_token):
        device = mm.get_torch_device()
        dtype = {"bf16": torch.bfloat16, "fp16": torch.float16, "fp32": torch.float32}[precision]

        model_name = model.rsplit('/', 1)[-1]
        model_path = os.path.join(folder_paths.models_dir, "lumina", model_name)
          
        if not os.path.exists(model_path):
            print(f"Downloading Lumina model to: {model_path}")
            from huggingface_hub import snapshot_download
            snapshot_download(repo_id=model,
                            ignore_patterns=['*ema*'],
                            local_dir=model_path)
                  
        train_args = torch.load(os.path.join(model_path, "model_args.pth"))

        gemma_path = os.path.join(folder_paths.models_dir, "LLM", "gemma-2b")
          
        if not os.path.exists(gemma_path):
            print(f"Downloading Gemma model to: {gemma_path}")
            from huggingface_hub import snapshot_download
            snapshot_download(repo_id="google/gemma-2b",
                            local_dir=gemma_path,
                            ignore_patterns=['*gguf*'],
                            token = hf_token)
            
        tokenizer = AutoTokenizer.from_pretrained(gemma_path)
        tokenizer.padding_side = "right"

        text_encoder = AutoModel.from_pretrained(gemma_path, torch_dtype=dtype, device_map=device).eval()
        cap_feat_dim = text_encoder.config.hidden_size

        model = models.__dict__[train_args.model](qk_norm=train_args.qk_norm, cap_feat_dim=cap_feat_dim)
        model.eval().to(dtype)

        sd = load_torch_file(os.path.join(model_path, "consolidated.00-of-01.safetensors"))
        model.load_state_dict(sd, strict=True)
        
        lumina_model = {
            'model': model, 
            'tokenizer': tokenizer, 
            'text_encoder': text_encoder,
            'train_args': train_args,
            'dtype': dtype
            }

        return (lumina_model,)

class LuminaGemmaTextEncode:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
            "lumina_model": ("LUMINAMODEL", ),
            "prompt": ("STRING", {"multiline": True, "default": "",}),
            "n_prompt": ("STRING", {"multiline": True, "default": "",}),
            },
        }
    
    RETURN_TYPES = ("LUMINATEMBED",)
    RETURN_NAMES =("lumina_embeds",)
    FUNCTION = "encode"
    CATEGORY = "LuminaWrapper"

    def encode(self, lumina_model, prompt, n_prompt):
        device = mm.get_torch_device()
        offload_device = mm.unet_offload_device()
        tokenizer = lumina_model['tokenizer']
        text_encoder = lumina_model['text_encoder']
        
        text_encoder.to(device)

        text_inputs = tokenizer(
            [prompt] + [n_prompt],
            padding=True,
            pad_to_multiple_of=8,
            max_length=256,
            truncation=True,
            return_tensors="pt",
        )

        text_input_ids = text_inputs.input_ids
        prompt_masks = text_inputs.attention_mask.to(device)

        prompt_embeds = text_encoder(
            input_ids=text_input_ids.to(device),
            attention_mask=prompt_masks.to(device),
            output_hidden_states=True,
        ).hidden_states[-2]

        text_encoder.to(offload_device)
        lumina_embeds = {
            'prompt_embeds': prompt_embeds,
            'prompt_masks': prompt_masks,
        }
        
        return (lumina_embeds,)

class LuminaT2ISampler:
    @classmethod
    def INPUT_TYPES(s):
        return {"required": {
            "latent": ("LATENT", ),
            "lumina_model": ("LUMINAMODEL", ),
            "lumina_embeds": ("LUMINATEMBED", ),
            "seed": ("INT", {"default": 0, "min": 0, "max": 0xffffffffffffffff}),
            "steps": ("INT", {"default": 50, "min": 1, "max": 200, "step": 1}),
            "cfg": ("FLOAT", {"default": 4.0, "min": 0.0, "max": 20.0, "step": 0.01}),
            "proportional_attn": ("BOOLEAN", {"default": False}),
            "do_extrapolation": ("BOOLEAN", {"default": False}),
            "scaling_watershed": ("FLOAT", {"default": 0.3, "min": 0.0, "max": 1.0, "step": 0.01}),
            "t_shift": ("INT", {"default": 4, "min": 1, "max": 20, "step": 1}),
            "solver": (
            [   'euler',
                'midpoint',
                'rk4',
            ],
            {
            "default": 'midpoint'
             }),
            },
        }
    
    RETURN_TYPES = ("LATENT",)
    RETURN_NAMES =("samples",)
    FUNCTION = "process"
    CATEGORY = "LuminaWrapper"

    def process(self, lumina_model, lumina_embeds, latent, seed, steps, cfg, proportional_attn, solver, t_shift, 
                do_extrapolation, scaling_watershed):
        device = mm.get_torch_device()
        offload_device = mm.unet_offload_device()

        model = lumina_model['model']
        dtype = lumina_model['dtype']
        
        z = latent["samples"]
        torch.manual_seed(seed)
        noise = torch.randn_like(z)
        z = z + noise
        z = z.repeat(2, 1, 1, 1)
        z = z.to(dtype).to(device)

        w = z.shape[3] * 8
        h = z.shape[2] * 8

        train_args = lumina_model['train_args']

        model_kwargs = dict(
                        cap_feats=lumina_embeds['prompt_embeds'],
                        cap_mask=lumina_embeds['prompt_masks'],
                        cfg_scale=cfg,
                    )
        if proportional_attn:
            model_kwargs["proportional_attn"] = True
            model_kwargs["base_seqlen"] = (train_args.image_size // 16) ** 2
            print(model_kwargs["base_seqlen"])
        else:
            model_kwargs["proportional_attn"] = False
            model_kwargs["base_seqlen"] = None

        if do_extrapolation:
            model_kwargs["scale_factor"] = math.sqrt(w * h / train_args.image_size**2)
            model_kwargs["scale_watershed"] = scaling_watershed
        else:
            model_kwargs["scale_factor"] = 1.0
            model_kwargs["scale_watershed"] = 1.0

        model.to(device)

        samples = ODE(steps, solver, t_shift).sample(z, model.forward_with_cfg, **model_kwargs)[-1]

        model.to(offload_device)
        samples = samples[:1]

        factor = 0.18215
        samples = samples / factor

        return ({'samples': samples},)   
     
NODE_CLASS_MAPPINGS = {
    "LuminaT2ISampler": LuminaT2ISampler,
    "DownloadAndLoadLuminaModel": DownloadAndLoadLuminaModel,
    "LuminaGemmaTextEncode": LuminaGemmaTextEncode
}
NODE_DISPLAY_NAME_MAPPINGS = {
    "LuminaT2ISampler": "Lumina T2I Sampler",
    "DownloadAndLoadLuminaModel": "DownloadAndLoadLuminaModel",
    "LuminaGemmaTextEncode": "Lumina Gemma Text Encode"
}