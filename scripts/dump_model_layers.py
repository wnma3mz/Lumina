import mlx.utils as mx_utils
import os
import importlib.util

def get_loader_class():
    # 直接加载文件以规避循环引用
    path = os.path.join(os.getcwd(), "lumina/providers/mlx_loader.py")
    spec = importlib.util.spec_from_file_location("mlx_loader", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.MlxModelLoader

def dump_layers(model_id, filename):
    print(f"\n--- Loading {model_id} ---")
    try:
        MlxModelLoader = get_loader_class()
        loader = MlxModelLoader(
            model_path=model_id,
            max_new_prefill_per_iter=4,
            use_builtin_batch_engine_fn=lambda: True,
            use_dedicated_batch_executor_fn=lambda: False,
            eos_ids_fn=lambda: set()
        )
        model, tokenizer, _, _ = loader.load(offload_vision=True, offload_audio=True, offload_embedding=True)
        
        all_params = mx_utils.tree_flatten(model.parameters())
        with open(filename, "w") as f:
            for name, _ in all_params:
                f.write(name + "\n")
        print(f"Successfully saved {len(all_params)} layers to {filename}")
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    dump_layers("mlx-community/Qwen3.5-0.8B-4bit", "qwen_layers_v2.txt")
    dump_layers("mlx-community/gemma-4-e2b-it-4bit", "gemma_layers_v2.txt")
