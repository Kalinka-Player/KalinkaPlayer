"""The valence/arousal head recipe Kalinka's deployed va_head_v1.onnx was trained with.

Linear(d,128) -> ReLU -> Dropout(0.3) -> Linear(128,2) in numpy, Adam, early
stopping on a validation split, labels z-scored on the training split. Run on
the release CLAP embeddings of DEAM with seed 1234 it reproduces the deployed
head's predictions to float precision (evaluate.py checks this).
"""
import numpy as np

SEED = 1234
HIDDEN = 128
DROPOUT = 0.3
LR = 1e-3
EPOCHS = 500
PATIENCE = 40
BATCH = 64
WEIGHT_DECAY = 1e-4
RIDGE_ALPHA = 10.0


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #
def rmse(y, p):
    return float(np.sqrt(np.mean((y - p) ** 2)))


def r2(y, p):
    ss_res = np.sum((y - p) ** 2)
    ss_tot = np.sum((y - np.mean(y)) ** 2)
    return float(1 - ss_res / ss_tot) if ss_tot > 0 else 0.0


def ccc(y, p):
    """Lin's concordance correlation coefficient."""
    my, mp = np.mean(y), np.mean(p)
    vy, vp = np.var(y), np.var(p)
    cov = np.mean((y - my) * (p - mp))
    denom = vy + vp + (my - mp) ** 2
    return float(2 * cov / denom) if denom > 0 else 0.0


# --------------------------------------------------------------------------- #
# Numpy MLP
# --------------------------------------------------------------------------- #
class MLP:
    def __init__(self, d_in, d_hidden, d_out, rng):
        # He init for the ReLU layer, Xavier for the linear output.
        self.W1 = (rng.standard_normal((d_in, d_hidden)) * np.sqrt(2 / d_in)).astype(np.float32)
        self.b1 = np.zeros(d_hidden, np.float32)
        self.W2 = (rng.standard_normal((d_hidden, d_out)) * np.sqrt(1 / d_hidden)).astype(np.float32)
        self.b2 = np.zeros(d_out, np.float32)
        self._init_adam()

    def _init_adam(self):
        self.m = {k: np.zeros_like(getattr(self, k)) for k in ("W1", "b1", "W2", "b2")}
        self.v = {k: np.zeros_like(getattr(self, k)) for k in ("W1", "b1", "W2", "b2")}
        self.t = 0

    def forward(self, X, drop_rng=None):
        z1 = X @ self.W1 + self.b1
        a1 = np.maximum(z1, 0.0)
        mask = None
        if drop_rng is not None and DROPOUT > 0:
            mask = (drop_rng.random(a1.shape) >= DROPOUT).astype(np.float32) / (1 - DROPOUT)
            a1 = a1 * mask
        out = a1 @ self.W2 + self.b2
        return out, (X, z1, a1, mask)

    def backward(self, cache, dout):
        X, z1, a1, mask = cache
        n = X.shape[0]
        gW2 = a1.T @ dout / n + WEIGHT_DECAY * self.W2
        gb2 = dout.mean(axis=0)
        da1 = dout @ self.W2.T
        if mask is not None:
            da1 = da1 * mask
        dz1 = da1 * (z1 > 0)
        gW1 = X.T @ dz1 / n + WEIGHT_DECAY * self.W1
        gb1 = dz1.mean(axis=0)
        return {"W1": gW1, "b1": gb1, "W2": gW2, "b2": gb2}

    def step(self, grads, lr, b1=0.9, b2=0.999, eps=1e-8):
        self.t += 1
        for k, g in grads.items():
            self.m[k] = b1 * self.m[k] + (1 - b1) * g
            self.v[k] = b2 * self.v[k] + (1 - b2) * (g * g)
            mhat = self.m[k] / (1 - b1 ** self.t)
            vhat = self.v[k] / (1 - b2 ** self.t)
            setattr(self, k, getattr(self, k) - lr * mhat / (np.sqrt(vhat) + eps))

    def predict(self, X):
        return self.forward(X, drop_rng=None)[0]
