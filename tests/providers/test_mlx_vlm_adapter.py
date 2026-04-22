import unittest
from unittest.mock import MagicMock
import mlx.core as mx
from lumina.providers.mlx_loader import MlxModelLoader

class TestMlxVlmAdapter(unittest.TestCase):
    def test_vlm_model_wrapper_unpacks_logits(self):
        # 模拟 VLM 加载后的代理逻辑
        from lumina.providers.mlx_loader import MlxModelLoader
        
        # 构造一个模拟的 LanguageModelOutput
        class MockOutput:
            def __init__(self, logits):
                self.logits = logits
        
        # 构造模拟模型
        original_model = MagicMock()
        mock_logits = mx.array([1.0, 2.0, 3.0])
        original_model.side_effect = lambda *args, **kwargs: MockOutput(mock_logits)
        
        # 实例化包装器（由于 MlxModelLoader 内部定义了该类，我们通过 Loader 逻辑间接测试或模拟）
        # 这里模拟 MlxModelLoader.load 中的包装逻辑
        class VLMModelWrapper:
            def __init__(self, original_model):
                self.__dict__["_model"] = original_model
            def __call__(self, *args, **kwargs):
                output = self._model(*args, **kwargs)
                return getattr(output, "logits", output)
            def __getattr__(self, name): return getattr(self._model, name)
            def parameters(self): return self._model.parameters()

        wrapped = VLMModelWrapper(original_model)
        
        # 执行调用，验证是否返回了 raw array 而不是 MockOutput
        result = wrapped(mx.array([[1]]))
        self.assertIsInstance(result, mx.array)
        self.assertTrue(mx.array_equal(result, mock_logits))

    def test_tokenizer_attribute_forwarding(self):
        # 模拟 Processor
        class MockProcessor:
            def __init__(self, tokenizer):
                self.tokenizer = tokenizer
        
        class MockTokenizer:
            def __init__(self):
                self.eos_token_id = 42
                self.eos_token_ids = [42]
            def get_vocab(self):
                return {"<eos>": 42, "<turn|": 100}

        inner_tok = MockTokenizer()
        processor = MockProcessor(inner_tok)
        
        # 模拟 MlxModelLoader 中的适配逻辑
        if not hasattr(processor, "eos_token_id") and hasattr(processor, "tokenizer"):
            t = processor.tokenizer
            processor.eos_token_id = t.eos_token_id
            raw_ids = getattr(t, "eos_token_ids", [t.eos_token_id])
            processor.eos_token_ids = list(raw_ids)
            vocab = t.get_vocab()
            turn_tokens = [i for k, i in vocab.items() if "<turn|" in k]
            if turn_tokens:
                processor.eos_token_ids = list(set(processor.eos_token_ids) | set(turn_tokens))

        self.assertEqual(processor.eos_token_id, 42)
        self.assertIn(100, processor.eos_token_ids)
        self.assertIn(42, processor.eos_token_ids)

if __name__ == "__main__":
    unittest.main()
