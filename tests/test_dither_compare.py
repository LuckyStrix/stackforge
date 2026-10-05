import unittest
import numpy as np

from stackforge.core import colormath


class BlueNoise(unittest.TestCase):
    def test_is_a_permutation_ranking(self):
        t = colormath.blue_noise(32)
        self.assertEqual(t.shape, (32, 32))
        self.assertEqual(len(np.unique(t)), 32 * 32)   # every threshold used once
        self.assertAlmostEqual(float(t.mean()), 0.5, places=2)

    def test_less_low_frequency_energy_than_white_noise(self):
        def lowf(a):
            f = np.fft.fftshift(np.abs(np.fft.fft2(a - a.mean())) ** 2)
            n = a.shape[0]
            y, x = np.mgrid[-n // 2:n // 2, -n // 2:n // 2]
            r = np.hypot(x, y)
            return f[(r > 0) & (r < 4)].sum() / f.sum()
        white = np.random.default_rng(1).random((64, 64))
        self.assertLess(lowf(colormath.blue_noise(64)), lowf(white) / 10)

    def test_deterministic(self):
        colormath._BLUE_CACHE.clear()
        a = colormath.blue_noise(16)
        colormath._BLUE_CACHE.clear()
        self.assertTrue(np.array_equal(a, colormath.blue_noise(16)))


if __name__ == "__main__":
    unittest.main()
