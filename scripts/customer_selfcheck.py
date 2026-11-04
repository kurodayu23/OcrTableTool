#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OcrTableTool 交付包 **客户侧自检**（纯标准库，无需安装任何第三方依赖）。

用途：客户把整包拷到目标机器后，双击 ``selfcheck.bat`` 或在命令行跑本脚本，
即可确认"这份包在这台机器上是不是完整可用"。

与打包流水线里的 ``scripts/verify_delivery_package.py`` 的区别：
那个脚本站在开发者一侧，**允许**读后端源码、装 PyInstaller 读 PYZ 归档；
本脚本站在客户一侧，**只依赖 Python 标准库**模型清单与样例图直接内嵌，
PYZ 内部条目改成扫描 exe 字节流里的模块标记。

判定是三态的：
  PASS  = 查过且通过
  FAIL  = 查过且不通过
  SKIP  = 这台机器上查不了（**不算通过**）
退出码：0 = 没有 FAIL；1 = 有 FAIL。

用法：
    python customer_selfcheck.py                 # 自动定位同目录下的交付包
    python customer_selfcheck.py --package-dir D:\\...\\ocr-runtime\\OcrBackend
"""

import argparse
import base64
import hashlib
import json
import marshal
import os
import struct
import subprocess
import sys
import tempfile
import zipfile
import zlib

# --------------------------------------------------------------------------
# 内嵌常量：随包一起交付，不依赖任何外部文件
# --------------------------------------------------------------------------

#: 15 个随包模型的 SHA-256 基线（与后端 MODEL_HASHES 逐字节一致）。
MODEL_HASHES = {
    "PP-OCRv6_det_medium.bin": "e219269e5bf6fc25a5298b70508b08794d53e0d8bfb70fc5c4def3d510464432",
    "PP-OCRv6_det_medium.xml": "31daceba46e13a709c500fb56e5e53d04118d0136193b08b82d19a04260af6ab",
    "PP-OCRv6_det_small.onnx": "090f04abcd9d9a7498bc4ebf677e4cb9bdce1fe4197ddb7e529f1ef44e1ff94f",
    "PP-OCRv6_rec_medium.bin": "68799c7cdf8fedae65d88f7d0613dd7dd2acda5a9b75255d4e6f5f870116f6ee",
    "PP-OCRv6_rec_medium.xml": "d69703799480895993719db4a24a0acef5d83f814da1816e472f261b46c6452e",
    "PP-OCRv6_rec_small.bin": "98eb31e6a921361182b5304de7507d16431f65435d4470c70ed7ae23e4dd2682",
    "PP-OCRv6_rec_small.xml": "eb313a2b4b5dd761c98e7cc5a5be88cf8900af602b2ee6041e88a423e0252011",
    "ch_PP-OCRv5_rec_mobile.onnx": "5825fc7ebf84ae7a412be049820b4d86d77620f204a041697b0494669b1742c5",
    "ch_PP-OCRv5_rec_server.onnx": "e09385400eaaaef34ceff54aeb7c4f0f1fe014c27fa8b9905d4709b65746562a",
    "ch_ppocr_mobile_v2.0_cls_mobile.bin": "f34e65c6e851cd417d5f13a1ccbbc362c831b0048b6f9cba984f2391eaea47e9",
    "ch_ppocr_mobile_v2.0_cls_mobile.xml": "9be8a3087ea24e220ac0260d820149f122f92f7f150fe480b91605b476620fd1",
    "en_PP-OCRv5_rec_mobile.onnx": "c3461add59bb4323ecba96a492ab75e06dda42467c9e3d0c18db5d1d21924be8",
    "latin_PP-OCRv5_dict.txt": "5ddc5c6086d61db3e1485f1237e059b89df9a228cebdac40a54406c6005bca67",
    "latin_PP-OCRv5_rec_mobile.onnx": "7888113072263cb471b93f66dd5e2ad70548dc526fa1ace760d0d973dd121498",
    "slanet-plus.onnx": "d57a942af6a2f57d6a4a0372573c696a2379bf5857c45e2ac69993f3b334514b",
}

#: 必须在冻结归档里存在的后端模块（缺一个就会静默走降级路径）。
REQUIRED_BACKEND_MODULES = (
    "fast_stats",
    "colored_table",
    "table_pipeline",
    "recognition_scheduler",
)

#: 表格结构引擎的模块路径标记。
RAPID_TABLE_ENGINE = "rapid_table.inference_engine.onnxruntime.main"

#: PyInstaller 冻结归档的常量（照 PyInstaller 6.x 的实现，纯标准库解析）。
_COOKIE_MAGIC = b"MEI\x0c\x0b\x0a\x0b\x0e"
_COOKIE_FORMAT = "!8siiii64s"
_TOC_ENTRY_FORMAT = "!IIIIBc"

#: 内嵌的样例表格图（900x520，6 行 5 列带框线），用来跑通识别链路。
SAMPLE_TABLE_PNG_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAA4QAAAIICAIAAAC5Kbd7AAAdD0lEQVR4nO3db2iV9/3/8c+JGibN"
    "iJbYxYKM/QFDdUyIJRSM5l5LWGoKNfoV0uMGlm2aVqprhdp7ldo6KcwK3pkSpZJNZ8OIuZEOalmQ"
    "7nYbHG2FIlUbNVtOY6XSxusHCuJvf9K0S/LOdfXxuLXlGuyz67Vz+vTKiSllWZYAACBCVch/KwAA"
    "iFEAACJ5MgoAQBgxCgBAGDEKAEAYMQoAQBgxCgBAGDEKAEAYMQoAQBgxCgBAGDEKAEAYMQoAQBgx"
    "CgBAGDEKAEAYMQoAQBgxCgBAGDEKAEAYMQoAQBgxCgBAGDEKAEAYMQoAQBgxCgBAGDEKAEAYMQoA"
    "QBgxCgBAGDEKAEAYMQoAQBgxCgBAGDEKAEAYMQoAQBgxCgBAGDEKAEAYMQoAQBgxCgBAGDEKAEAY"
    "MQoAQBgxCgBAGDEKAEAYMQoAQBgxCgBAGDEKAEAYMQoAQBgxCgBAGDEKAEAYMQoAQBgxCgBAGDEK"
    "AEAYMQoAQBgxCgBAGDEKAEAYMQoAQBgxCgBAGDEKAEAYMQoAQBgxCgBAGDEKAEAYMQoAQBgxCgBA"
    "mLkph0qlUvQRAABmoyzLUq7k78moEgUAKEwp5S9GAQAojFx+mz6nT6H5yj/D2bQwbFo8Ni0emxZP"
    "KW/PRG/zZBQAgDBiFACAMGIUAIAwYhQAgDBiFACAMGIUAIAwYhQAgDBiFACAMGIUAIAwhY3RSqXS"
    "3t6+atWq9vb2SqUyyUvMZhMPNzo6umnTptra2qDTMfWbHjp0qLm5ecWKFQMDA+5vATa9fv36+vXr"
    "W1paGhsbT506FXdGvp7J/EOzv79//vz57izfTGFjdPfu3atXrx4cHGxubn7ppZcmeYnZbOLh2tra"
    "Ghsbc/qb0L61Jtj0ypUr3d3db7/9dk9Pz9NPPx13RqZs09dee+3BBx88ffp0X1/fli1b3Nm8+Mp/"
    "aI6Njb344ovz5s2LOB2FkOXNJE++bNmyCxcuZFn28ccfL1++fJKXyOmmWZZdunQpy7La2trpPCkz"
    "t+nZs2f/+Mc/Zll27dq1RYsWufUF2PQf//jHjRs3six78803f/zjH0/zeZmh994sy37961//4Q9/"
    "8PY7G6R81l1hn4wODw/X19enlBYvXjw8PDzJS8xmEw93+xKF2bShoWHdunUppRMnTrS1tcWdkSnb"
    "dOHChdXV1Z2dnWvXrj148KA7W4z33sHBwYsXL3Z0dASdjiIobIz+u127drW0tLzxxhvRB2HK2LTw"
    "m547d27v3r179uyJPhdTtunRo0ePHTvW3d3tnhZg0xs3buzYsePAgQPRJyLnsryZ5Ml9m/5btekd"
    "vk9UpE3HxsYaGxvfeeed6TwpM7fp1q1bv/jiiyzLvvzyy3vvvdetL8Cmr7/++gMPPLDmljlz5nR2"
    "dk7zkfkKOa27wj4ZbW1t7enpSSn19PS0trZO8hKzmeG+VZtmWVYul7dv397U1BR3QKZy00ql0tvb"
    "m1I6c+bM0qVL3dwCbLpx48ahoaHTt9TU1Bw5ciTumORY6e6OzoU7Py498ckrlUq5XL569WpdXV13"
    "d/fdf+PPBJfI6aZ3LFiwYHR0dNpOysxtevjw4a6urpUrV6aUampq+vr63P28b3r+/PlyuXzz5s3q"
    "6ur9+/c3NDRM/6mZofdeb7/52nS2KWyMkiM2LR6bFo9Ni8emxVPKZyMV9tv0AADMfmIUAIAwYhQA"
    "gDBiFACAMGIUAIAwYhQAgDBiFACAMGIUAIAwYhQAgDBiFACAMGIUAIAwYhQAgDBiFACAMGIUAIAw"
    "YhQAgDBiFACAMGIUAIAwYhQAgDBiFACAMGIUAIAwYhQAgDBiFACAMGIUAIAwYhQAgDBiFACAMGIU"
    "AIAwYhQAgDBiFACAMGIUAIAwYhQAgDBiFACAMGIUAIAwYhQAgDBiFACAMGIUAIAwYhQAgDBiFACA"
    "MGIUAIAwYhQAgDBiFACAMGIUAIAwYhQAgDBiFACAMGIUAIAwpSzLUq6USqXoIwAAzF5ZrurOk1EA"
    "AMKIUQAAwsxNuZWvR9BM5qMXNi0MmxaPTYvHpsVTyudHGT0ZBQAgjBgFACCMGAUAIIwYBQAgjBgF"
    "ACCMGAUAIIwYBQAgjBgFACCMGAUAIEyRY3R0dHTTpk21tbX/8vVKpdLe3r5q1ar29vZKpRJ0OqZy"
    "04kvMZtNMNyhQ4eam5tXrFgxMDAQcTSmeNPr16+vX7++paWlsbHx1KlT7m+OfOUbbH9///z582f2"
    "UBRHkWO0ra2tsbHx33811u7du1evXj04ONjc3PzSSy8FnY6p3HTiS8xm/224K1eudHd3v/322z09"
    "PU8//XTQ6ZjKTV977bUHH3zw9OnTfX19W7ZscXNzZOI32LGxsRdffHHevHkzfi4KopS73wY++d+l"
    "+8knn9TX1y9YsGB0dPTury9fvnxgYOD++++/cOHCI4888u67707neZmJTSe+RB43/fvf//7uu++u"
    "W7fus88++8EPfnD58uXpPC8zsek///nPe+65p7q6+i9/+cuvfvWrDz74wH0vwHtvSmnLli1r1qx5"
    "8sknvf3maNNZpchPRuvr6//j14eHh29fWrx48fDw8Iyfi6nfdOJLzGb/bbiGhoZ169allE6cONHW"
    "1jbj52LqN124cGF1dXVnZ+fatWsPHjzoFufIBG+wg4ODFy9e7OjomNkTUShFjtF/sWvXrpaWljfe"
    "eCP6IEwZmxZ+03Pnzu3du3fPnj3R52LKNj169OixY8e6u7vd0wJseuPGjR07dhw4cCD6RORbkb9N"
    "f5tv038bNp3MJXK36bVr11paWg4cONDU1DQNZ2SmN+3q6nr11Vfnzp07Pj5+3333jYyM2CDvmx47"
    "dmz37t2LFi26/Yh048aNR44cmZ7DMim+TZ8bra2tPT09KaWenp7W1tbo4wD/QZZl5XJ5+/btSrQw"
    "KpVKb29vSunMmTNLly6NPg5TYOPGjUNDQ6dvqampUaJ8M9/GJ6OVSqVcLl+9erWurq67u9tfBhTO"
    "k9Hi+d83PXz4cFdX18qVK1NKNTU1fX1903NSZm7T8+fPl8vlmzdvVldX79+/v6Ghwd0vzHuvb0zN"
    "EqV8/gBT8WOU2c+mxWPT4rFp8di0eEr5bKRv0Q8wAQAw24hRAADCiFEAAMKIUQAAwohRAADCiFEA"
    "AMKIUQAAwohRAADCiFEAAMKIUQAAwohRAADCiFEAAMKIUQAAwohRAADCiFEAAMKIUQAAwohRAADC"
    "iFEAAMKIUQAAwohRAADCiFEAAMKIUQAAwohRAADCiFEAAMKIUQAAwohRAADCiFEAAMKIUQAAwohR"
    "AADCiFEAAMKIUQAAwohRAADCiFEAAMKIUQAAwohRAADCiFEAAMKIUQAAwohRAADCiFEAAMKIUQAA"
    "wohRAADCiFEAAMKIUQAAwohRAADCiFEAAMKUsixLuVIqlaKPAAAwe2W5qjtPRgEACCNGAQAIMzfl"
    "Vr4eQTOZj17YtDBsWjw2LR6bFk8pnx9l9GQUAIAwYhQAgDBiFACAMGIUAIAwYhQAgDBiFACAMGIU"
    "AIAwYhQAgDBiFACAMEWO0UOHDjU3N69YsWJgYODur1cqlfb29lWrVrW3t1cqlbgDMmWbppRGR0c3"
    "bdpUW1vrthZm0wkuMZv9t+GuX7++fv36lpaWxsbGU6dOxR2Qr+0rX4z9/f3z5893Z/mGsryZ5Mkv"
    "X768evXq8fHxs2fPNjQ03H3pN7/5zb59+7Is++1vf/vcc89N83mZiU2zLFu1atXvfve72tpad7wY"
    "m048N3nc9OWXX967d2+WZRcvXvz+978/zedlht57syz79NNPH3rooe9+97tueriUz7or7JPRkZGR"
    "rVu3VlVVLVmyZGRk5O5L/f39GzZsSClt2LDBn86LsWlK6fjx411dXUFHY+o3nXhuZq0Jhtu8efNT"
    "Tz2VUhoaGpo3b17cGfl6vvLFuHPnzm3btlVVFbYomG5zU0E13JJSOnHiRFtb292XhoeH6+vrU0qL"
    "Fy8eHh6OOyNTtmlK6famFGbTiedm1ppguIULF6aUOjs7T548+ec//znujHw9E78YBwcHL1682NHR"
    "8eSTT7qzfENZ3nytk3/44YfLli27fPlylmXPP//8mjVrTp48WVdXNz4+nmXZ+Pj4okWLpv/ITPum"
    "d676Nn3BNr37EoV5nfb29nZ2dk7nYZmhTT///POmpqYLFy54+50lUj7rLhX4Ro+NjTU2Nr7zzjv/"
    "8vVly5bdfuV8/PHHy5cvn7aTMnOb3iFGi7TpV85NvjbdunXrF198kWXZl19+ee+9907bSZm5TV9/"
    "/fUHHnhgzS1z5szxZ4xwKZ8xWthPeGRZVi6Xt2/f3tTU9C+XWltbe3p6Uko9PT2tra1BB2QqN6V4"
    "m5o7pyYYrlKp9Pb2ppTOnDmzdOnSoAMylZtu3LhxaGjo9C01NTVHjhxxf/kGSnd3dC6USqXb/2Li"
    "kx8+fLirq2vlypUppZqamr6+vjuXKpVKuVy+evVqXV1dd3e3vwyoAJvesWDBgtHR0Wk7KTO36WTm"
    "Jl+bnj9/vlwu37x5s7q6ev/+/bc/hkgx3nu9/eZr09mmsDFKjti0eGxaPDYtHpsWTymfjVTYb9MD"
    "ADD7iVEAAMKIUQAAwohRAADCiFEAAMKIUQAAwohRAADCiFEAAMKIUQAAwohRAADCiFEAAMKIUQAA"
    "wohRAADCiFEAAMKIUQAAwohRAADCiFEAAMKIUQAAwohRAADCiFEAAMKIUQAAwohRAADCiFEAAMKI"
    "UQAAwohRAADCiFEAAMKIUQAAwohRAADCiFEAAMKIUQAAwohRAADCiFEAAMKIUQAAwohRAADCiFEA"
    "AMKIUQAAwohRAADCiFEAAMKIUQAAwohRAADCiFEAAMKIUQAAwohRAADCiFEAAMKIUQAAwpSyLEu5"
    "UiqVoo8AADB7ZbmqO09GAQAII0YBAAgzN+VWvh5BM5mPXti0MGxaPDYtHpsWTymfH2X0ZBQAgDBi"
    "FACAMGIUAIAwYhQAgDBiFACAMGIUAIAwYhQAgDBiFACAMGIUAIAwhY3R69evr1+/vqWlpbGx8dSp"
    "U3dfqlQq7e3tq1atam9vr1QqcWdkyjZNKY2Ojm7atKm2ttZtLcymhw4dam5uXrFixcDAQNABmcpN"
    "J56bWWsyw/X398+fP3/Gj0ZBlHL3Cxgn+evLXnnllaqqqh07dly6dOmhhx766KOP7lx69tln6+vr"
    "n3nmmX379l25cmXPnj3Tf2qmd9OUUnNzc0dHxwsvvDA6Oup2F2DTK1euPP7442+99db777//2GOP"
    "nT17dvpPzfRuOvFLmJy+96aUxsbGHn744ffee+/TTz+dzvNS2F/xmuPfTT+xzZs333PPPSmloaGh"
    "efPm3X2pv7//9oOWDRs2PPLII2K0AJumlI4fP15fX//CCy8EnY4p3nRkZGTr1q1VVVVLliwZGRlx"
    "fwuw6cQvYWatrxxu586d27Zte/LJJyNORxEUNkYXLlyYUurs7Dx58uSf//znuy8NDw/X19enlBYv"
    "Xjw8PBx3RqZs05TS7U0pzKYNt6SUTpw40dbWFndGpmzTiV/CzFoTDzc4OHjx4sWOjg4xyjeX5c3X"
    "PXlvb29nZ2eWZc8///yaNWtOnjxZV1c3Pj6eZdn4+PiiRYum+bzMxKZ3LtXW1rrjRdr0ww8/XLZs"
    "2eXLl6fzsMz06/TOJfK+6eeff97U1HThwgVvv7NEymfdpaLe6K1bt37xxRdZln355Zf33nvv3ZeW"
    "LVt2+5Xz8ccfL1++fJrPy0xseocYLdKmY2NjjY2N77zzznSelJnbdDIvYfK16euvv/7AAw+suWXO"
    "nDn+jBEu5TNGC/vT9JVKpbe3N6V05syZpUuX3n2ptbW1p6cnpdTT09Pa2hp3RqZsU4q3aZZl5XJ5"
    "+/btTU1NcQdkKjf1Es6pCYbbuHHj0NDQ6VtqamqOHDkSd0xyrLA/TX/+/PlyuXzz5s3q6ur9+/ff"
    "/vDZbZVKpVwuX716ta6urru7218GVIBN71iwYIGfpi/GpocPH+7q6lq5cmVKqaampq+vb/pPzfRu"
    "OpmXMDl97/X2O0uU8vnT9IWNUXLEpsVj0+KxafHYtHhK+Wykwn6bHgCA2U+MAgAQRowCABBGjAIA"
    "EEaMAgAQRowCABBGjAIAEEaMAgAQRowCABBGjAIAEEaMAgAQRowCABBGjAIAEEaMAgAQRowCABBG"
    "jAIAEEaMAgAQRowCABBGjAIAEEaMAgAQRowCABBGjAIAEEaMAgAQRowCABBGjAIAEEaMAgAQRowC"
    "ABBGjAIAEEaMAgAQRowCABBGjAIAEEaMAgAQRowCABBGjAIAEEaMAgAQRowCABBGjAIAEEaMAgAQ"
    "RowCABBGjAIAEEaMAgAQRowCABBGjAIAEEaMAgAQppRlWcqVUqkUfQQAgNkry1XdeTIKAEAYMQoA"
    "QJi5Kbfy9QiayXz0wqaFYdPisWnx2LR4Svn8KKMnowAAhBGjAACEEaMAAIQRowAAhBGjAACEEaMA"
    "AIQRowAAhBGjAACEEaMAAIQpfoz29/fPnz//7q9UKpX29vZVq1a1t7dXKpW4ozFlm6aURkdHN23a"
    "VFtb67YWZtNDhw41NzevWLFiYGAg6FxM5abXr19fv359S0tLY2PjqVOn3NxivE6/8hJ822N0bGzs"
    "xRdfnDdv3t1f3L179+rVqwcHB5ubm1966aW40zFlm6aU2traGhsbc/qb0L7l/uOmV65c6e7ufvvt"
    "t3t6ep5++um40/FN/MdNX3vttQcffPD06dN9fX1btmxxZ4vx3jvxJZiMgsfozp07t23bVlX1//3P"
    "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"
    "8OHDXV1dK1euTCnV1NTc/mchud70/Pnz5XL55s2b1dXV+/fvb2homP5TM0Pvvd5+Z4lSPn+avrAx"
    "So7YtHhsWjw2LR6bFk8pn41U2B9gAgBg9hOjAACEEaMAAIQRowAAhBGjAACEEaMAAIQRowAAhBGj"
    "AACEEaMAAIQRowAAhBGjAACEEaMAAIQRowAAhBGjAACEEaMAAIQRowAAhBGjAACEEaMAAIQRowAA"
    "hBGjAACEEaMAAIQRowAAhBGjAACEEaMAAIQRowAAhBGjAACEEaMAAIQRowAAhBGjAACEEaMAAIQR"
    "owAAhBGjAACEEaMAAIQRowAAhBGjAACEEaMAAIQRowAAhBGjAACEEaMAAIQRowAAhBGjAACEEaMA"
    "AIQRowAAhBGjAACEEaMAAIQpZVmWcqVUKkUfAQBg9spyVXeejAIAECZ/MZqv2AcAmElZ3kopf9+m"
    "BwCgMPL3ZBQAgMIQowAAhBGjAACEEaMAAIQRowAAhBGjAACEEaMAAIQRowAAhBGjAACEEaMAAIQR"
    "owAAhBGjAACEEaMAAIQRowAAhBGjAACEEaMAAIQRowAAhBGjAACEEaMAAIQRowAAhBGjAACEEaMA"
    "AIQRowAAhBGjAACEEaMAAIQRowAAhBGjAACEEaMAAIQRowAAhBGjAACEEaMAAIQRowAAhBGjAACE"
    "EaMAAIQRowAAhBGjAACEEaMAAIQRowAAhBGjAACEEaMAAIQRowAAhBGjAACEEaMAAIQRowAAhBGj"
    "AACEEaMAAIQRowAAhBGjAACEEaMAAIQRowAAhBGjAACEEaMAAIQRowAAhBGjAACEEaMAAIQRowAA"
    "hBGjAACkKP8PK1dliNP5C2sAAAAASUVORK5CYII="
)


def _frozen_module_names(executable):
    """列出冻结归档里的模块名（纯标准库，不需要安装 PyInstaller）。

    交付包里"某个后端模块没被打进去"是**静默**故障：
    ``colored_table`` 缺了会自动回落到 ``np.median``，程序照样跑、结果悄悄变差。
    所以客户机上也要能查。

    返回模块名集合；**解析不了时返回 None**（格式变了），由调用方记 SKIP ——
    查不了不等于查过不通过，也不等于通过。
    """
    try:
        with open(executable, "rb") as handle:
            data = handle.read()
    except OSError:
        return None
    cookie_pos = data.rfind(_COOKIE_MAGIC)
    if cookie_pos < 0:
        return None
    try:
        # 2026-10-30（lead 复查 P1）：这里原来写死切片 96 字节，而
        # `_COOKIE_FORMAT = "!8siiii64s"` 实际只有 88 字节（struct.calcsize）。
        # 交付 exe 的 cookie 恰好贴在文件末尾时才侥幸取到 88 字节；一旦 exe 被
        # Authenticode 签名或追加数据，切片就是 96 字节 ⇒ struct.error ⇒ 本函数
        # 返回 None ⇒ 主程序/4 个后端模块/结构引擎共 6 项一起降级成 1 条 SKIP，
        # 而脚本仍以退出码 0 结束，门禁看起来还是"通过"。
        cookie_size = struct.calcsize(_COOKIE_FORMAT)
        (_magic, archive_length, toc_offset, toc_length,
         _pyvers, _pylib) = struct.unpack(
            _COOKIE_FORMAT, data[cookie_pos:cookie_pos + cookie_size])
    except struct.error:
        return None
    pkg_start = len(data) - archive_length
    toc_data = data[pkg_start + toc_offset:pkg_start + toc_offset + toc_length]
    entries = {}
    cursor = 0
    try:
        while cursor < len(toc_data):
            (entry_length, entry_offset, data_length, _uncompressed,
             compression_flag, typecode) = struct.unpack(
                _TOC_ENTRY_FORMAT, toc_data[cursor:cursor + 18])
            cursor += 18
            name_length = entry_length - 18
            if name_length < 0:
                return None
            name = toc_data[cursor:cursor + name_length].rstrip(b"\0").decode("utf-8")
            cursor += name_length
            entries[name] = (entry_offset, data_length, compression_flag,
                             typecode.decode("ascii"))
    except (struct.error, UnicodeDecodeError):
        return None

    names = set(entries)
    for _name, (entry_offset, data_length, compression_flag, typecode) in entries.items():
        if typecode != "z":
            continue
        raw = data[pkg_start + entry_offset:pkg_start + entry_offset + data_length]
        if compression_flag:
            try:
                raw = zlib.decompress(raw)
            except zlib.error:
                continue
        if raw[:4] != b"PYZ\0":
            continue
        try:
            toc_position = struct.unpack("!i", raw[8:12])[0]
            items = marshal.loads(raw[toc_position:])
        except Exception:  # noqa: BLE001 - 解析不了就当查不了
            continue
        for item in items:
            if isinstance(item, (tuple, list)) and item:
                names.add(str(item[0]))
            else:
                names.add(str(item))
    return names


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_sample_table(path):
    with open(path, "wb") as handle:
        handle.write(base64.b64decode(SAMPLE_TABLE_PNG_BASE64))


def _safe_working_directory(executable):
    """返回可作 `cwd` 的目录；**UNC 路径不能当 cwd**。

    2026-10-30（lead 复查 P2）：`cwd=str(exe的目录)` 在 UNC 路径（`\\\\server\\share\\...`，
    客户从网络共享直接跑自检）下会抛 `NotADirectoryError [WinError 267]`，
    于是 health / 导出 XLSX / 识别三条实跑全部 FAIL，被误判成"包坏了"。
    这里对 UNC 直接返回 None（`subprocess` 用当前目录），EXE 本身仍用绝对路径启动。
    """
    directory = os.path.dirname(str(executable))
    if not directory:
        return None
    if directory.startswith("\\\\") or directory.startswith("//"):
        return None
    return directory


def _run_executable(executable, request, timeout):
    payload = json.dumps(request, ensure_ascii=False).encode("utf-8")
    completed = subprocess.run(
        [str(executable)],
        input=payload,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        cwd=_safe_working_directory(executable),
    )
    text = completed.stdout.decode("utf-8", "replace").strip()
    if not text:
        detail = completed.stderr.decode("utf-8", "replace").strip()[-600:]
        raise RuntimeError("后端没有返回任何输出%s" % ("：" + detail if detail else ""))
    document = json.loads(text.splitlines()[-1])
    if not isinstance(document, dict):
        raise RuntimeError("后端返回的不是 JSON 对象")
    return document


# 2026-10-30（lead 复查 P2）：判据原来只做「告警非空且不含『结构模型』」——
#   ① 「线框、文字布局与结构模型结果不一致」也含"结构模型"，会被误判成引擎不可用；
#   ② 反过来，两条与引擎无关的复核告警（"未确认单元格覆盖范围较大…"）就能让这一项
#      PASS，而引擎可能根本没参与 ⇒ 判据与"引擎是否可用"没有因果关系。
# 现在分开写：失败只认引擎真的不可用；通过必须有**正向证据**。
_ENGINE_UNAVAILABLE_MARKERS = ("表格结构模型本次不可用", "结构模型本次不可用")
_ENGINE_RAN_MARKERS = ("线框、文字布局与结构模型结果不一致",)


def _structure_engine_state(response):
    """三态判定表格结构引擎是否真的可用。

    PASS 需要正向证据（引擎参与过或结构已被验证），否则记 SKIP ——
    「查不了"不等于"查过且通过"。
    """
    warnings = response.get("structure_warnings")
    if warnings is None:
        return None, "响应里没有 structure_warnings 字段，无法确认结构引擎跑过"
    warnings = [str(item) for item in warnings]
    broken = [
        item
        for item in warnings
        if any(marker in item for marker in _ENGINE_UNAVAILABLE_MARKERS)
    ]
    if broken:
        return False, broken[0][:160]
    if any(marker in item for item in warnings for marker in _ENGINE_RAN_MARKERS):
        return True, "结构引擎参与且未报告不可用"
    if response.get("structure_verified") is True:
        return True, "结构已按物理边界验证（structure_verified=true）"
    return None, "没有证据说明结构引擎参与过（告警与引擎无关，且结构未验证）"


def _default_package_dir():
    here = os.path.dirname(os.path.abspath(__file__))
    for candidate in (
        os.path.join(here, "ocr-runtime", "OcrBackend"),
        os.path.join(here, "OcrBackend"),
        os.path.join(os.path.dirname(here), "OcrBackend"),
    ):
        if os.path.isfile(os.path.join(candidate, "OcrBackend.exe")):
            return candidate
    return os.path.join(here, "ocr-runtime", "OcrBackend")


def main():
    parser = argparse.ArgumentParser(description="OcrTableTool 交付包客户侧自检")
    parser.add_argument("--package-dir", default=None, help="交付包目录（含 OcrBackend.exe）")
    parser.add_argument("--timeout", type=int, default=300, help="单条请求超时秒数")
    arguments = parser.parse_args()

    # Windows 控制台默认是 GBK，中文输出会变成乱码。统一改成 UTF-8；
    # 自检脚本还要能被重定向到文件，所以不能只靠控制台代码页。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    package = os.path.abspath(
        arguments.package_dir if arguments.package_dir else _default_package_dir()
    )
    executable = os.path.join(package, "OcrBackend.exe")
    passed = []
    failed = []
    skipped = []

    def check(name, condition, detail=""):
        if condition is None:
            skipped.append("SKIP %s%s" % (name, (" - " + detail) if detail else ""))
        elif condition:
            passed.append("PASS %s%s" % (name, (" - " + detail) if detail else ""))
        else:
            failed.append("FAIL %s%s" % (name, (" - " + detail) if detail else ""))

    print("OcrTableTool 交付包自检")
    print("交付包目录：%s" % package)
    print("")

    check("交付包目录存在", os.path.isdir(package), package)
    check("OcrBackend.exe 存在", os.path.isfile(executable), executable)
    if not os.path.isfile(executable):
        for line in failed + skipped:
            print(line)
        print("")
        print("自检失败：找不到 OcrBackend.exe，请确认整包已完整拷贝。")
        return 1

    # ---- 模型 ------------------------------------------------------------
    model_dir = os.path.join(package, "models")
    check("models 目录存在", os.path.isdir(model_dir), model_dir)
    if os.path.isdir(model_dir):
        missing = [n for n in MODEL_HASHES if not os.path.isfile(os.path.join(model_dir, n))]
        check("15 个模型齐全", not missing, ("缺: " + ", ".join(sorted(missing)[:6])) if missing else "")
        mismatched = []
        for name, expected in MODEL_HASHES.items():
            path = os.path.join(model_dir, name)
            if not os.path.isfile(path):
                continue
            if _sha256(path) != expected:
                mismatched.append(name)
        check("模型 SHA-256 与基线一致", not mismatched,
              ("不符: " + ", ".join(sorted(mismatched)[:6])) if mismatched else "")
        extra = sorted(
            n for n in os.listdir(model_dir)
            if os.path.isfile(os.path.join(model_dir, n)) and n not in MODEL_HASHES
        )
        check("models 目录无多余文件", not extra,
              ("多: " + ", ".join(extra[:6])) if extra else "")

    # ---- 词典与授权文件 ---------------------------------------------------
    for name in ("en_US.dic", "SCOWL-LICENSE.txt"):
        check("%s 随包" % name, os.path.isfile(os.path.join(package, name)))

    # ---- 原生库与冻结归档里的模块 ----------------------------------------
    native = os.path.join(
        package, "_internal", "onnxruntime", "capi", "onnxruntime_pybind11_state.pyd"
    )
    check("onnxruntime 原生库存在", os.path.isfile(native), native)

    frozen = _frozen_module_names(executable)
    if frozen is None:
        check("冻结归档可解析", None, "无法解析冻结归档（打包格式可能已变更）")
    else:
        check("主程序 ocr_backend 已打包", "ocr_backend" in frozen)
        for module in REQUIRED_BACKEND_MODULES:
            check("后端模块 %s 已打包" % module, module in frozen, module)
        check("表格结构引擎在冻结归档内", RAPID_TABLE_ENGINE in frozen, RAPID_TABLE_ENGINE)

    # ---- 实跑三条链路 ----------------------------------------------------
    try:
        response = _run_executable(executable, {"protocol": 1, "action": "health"},
                                   arguments.timeout)
        check("health 动作",
              response.get("status") == "ok" and bool(response.get("models_valid")),
              "status=%s models_valid=%s" % (response.get("status"),
                                             response.get("models_valid")))
    except Exception as error:  # noqa: BLE001
        check("health 动作", False, str(error))

    workdir = tempfile.mkdtemp(prefix="ocr-selfcheck-")
    try:
        output = os.path.join(workdir, "self-check.xlsx")
        request = {
            "protocol": 1,
            "action": "export_xlsx",
            "output_path": output,
            "cells": [
                [{"text": "序号", "confidence": 0.99}, {"text": "名称", "confidence": 0.99}],
                [{"text": "1", "confidence": 0.99}, {"text": "示例", "confidence": 0.99}],
            ],
            "spans": [],
        }
        try:
            response = _run_executable(executable, request, arguments.timeout)
            produced = os.path.isfile(output) and zipfile.is_zipfile(output)
            check("导出 XLSX 动作", response.get("status") == "ok" and produced,
                  "status=%s 生成文件=%s" % (response.get("status"), produced))
        except Exception as error:  # noqa: BLE001
            check("导出 XLSX 动作", False, str(error))

        image = os.path.join(workdir, "table.png")
        _write_sample_table(image)
        request = {
            "protocol": 1,
            "action": "recognize",
            "image_path": image,
            "output_directory": os.path.join(workdir, "out"),
            "options": {"crop_mode": "auto"},
        }
        try:
            response = _run_executable(executable, request, arguments.timeout)
            rows = int(response.get("rows", 0) or 0)
            columns = int(response.get("columns", 0) or 0)
            check("识别动作", response.get("status") == "ok" and rows > 0 and columns > 0,
                  "status=%s %sx%s" % (response.get("status"), rows, columns))
            state, detail = _structure_engine_state(response)
            check("表格结构引擎可用", state, detail)
        except Exception as error:  # noqa: BLE001
            check("识别动作", False, str(error))
    finally:
        try:
            import shutil
            shutil.rmtree(workdir, ignore_errors=True)
        except Exception:  # noqa: BLE001
            pass

    for line in passed:
        print(line)
    for line in failed:
        print(line)
    for line in skipped:
        print(line)
    print("")
    print("自检结果：通过 %d 项，失败 %d 项，跳过 %d 项" % (len(passed), len(failed), len(skipped)))
    if failed:
        print("结论：不通过。请把上面的 FAIL 行连同本机信息发给技术支持。")
        return 1
    if skipped:
        print("结论：通过（其中 %d 项在本机无法验证，已标记 SKIP，不计入通过）。" % len(skipped))
        return 0
    print("结论：通过。这份交付包在这台机器上完整可用。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
