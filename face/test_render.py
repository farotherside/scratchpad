#!/usr/bin/env python3
import sys
sys.path.insert(0, '.')
from core.face_model import FaceParams
from core.renderer import render
import numpy as np

params = FaceParams()
buf = render(40, 20, params)
print('buf shape:', buf.shape)
print('min:', buf.min(), 'max:', buf.max(), 'nonzero:', np.count_nonzero(buf))

ramp = ' .:-=+*#%@'
for row in buf:
    print(''.join(ramp[int(v * (len(ramp)-1))] for v in row))
