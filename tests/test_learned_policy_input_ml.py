"""PyTorch-specific Learned Policy input adapter tests."""

import importlib.util
import unittest

from _learned_policy_input_fixtures import complex_policy_input

from lisjong_arena.learned_policy_input import (
    FEATURE_DIM,
    build_policy_input_feature,
    to_tensor,
)

TORCH_AVAILABLE = importlib.util.find_spec("torch") is not None


@unittest.skipUnless(TORCH_AVAILABLE, "requires the Arena ml extra")
class LearnedPolicyInputMlTest(unittest.TestCase):
    def test_to_tensor_is_one_finite_float32_vector(self):
        import torch

        value = to_tensor(build_policy_input_feature(complex_policy_input()))
        self.assertEqual(value.shape, (FEATURE_DIM,))
        self.assertIs(value.dtype, torch.float32)
        self.assertTrue(bool(torch.isfinite(value).all()))


if __name__ == "__main__":
    unittest.main()
