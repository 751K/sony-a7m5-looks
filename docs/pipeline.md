# Sony Imaging Edge Edit 的 RAW 处理管线（A7M5）

本文只记录最终结论：各阶段做什么、参数从哪里来、移植在哪里、怎样验证。推导过程和被推翻的中间结论都在 git 历史里（2026-10-18 之前的版本）。文中的 `verify_*.py` 脚本、模拟器和证据文件属于研究工作区，不在本仓库。

- **目标程序**：Imaging Edge Edit 4.0.00.10312，arm64 切片 SHA-256 `a03b2449…5da442`。4.1.00.03062 的颜色链与之相同（§10.2）。
- **验证方式**：在 Unicorn 模拟器里运行 Edit 的原始 ARM64 函数，使用 Sony 自己的 ARW 解析器和对象，与 Python 移植逐位比较；整图再与 Edit 的导出图比较。
- **函数地址**均指 4.0.00.10312。`dev+X` 是显影参数结构，`look+X` 是解析器生成的外观结构，`render+X` 是渲染上下文。

## 0. 现状

| 阶段 | 移植 | 默认运行 | 进入配置文件 | 节 |
| --- | --- | --- | --- | --- |
| 前端：黑电平、缩放、白平衡（全部模式） | `sony_frontend.py` | 是 | 否（宿主软件负责） | §2 |
| RawNR | `sony_rawnr.py` | 按设置 | 否 | §3.1 |
| ITP 去马赛克 | `sony_itp.py` | 是 | 否 | §3.2 |
| DRO（ZcTaskVatr） | `sony_dro.py` | 按设置 | 否 | §3.3 |
| GeoCorr（畸变 + 横向色差） | `sony_lens.py` | 按设置 | 否 | §3.4 |
| SSCS | `sony_sscs.py` | 是 | 是 | §3.5 |
| LinearMatrix16、MainGamma | `sony_look.py` | 是 | 是 | §4.1、§4.2 |
| RGB2YCC、AreaComp、ChromaSuppress | `sony_ycc.py`、`sony_ycc_stages.py` | 是 | 是 | §4.3–§4.5 |
| YGamma（含褪色） | `sony_ycc_stages.py` | 是 | 是 | §4.6 |
| HueSaturation | `sony_ycc_stages.py` | 滑块非 0 时 | 否 | §4.7 |
| 3DLut | 只有应用部分 | 否（0x8026 = 0） | — | §4.8 |
| YCC2RGB | `sony_ycc.py` | 是 | 是 | §4.3 |
| Sharpness、Spica | `sony_detail.py` | 是 | 否 | §5.1、§5.2 |
| Marble | `sony_marble.py` | 按设置 | 否 | §5.3 |
| Effect（BW / SE） | `sony_effect.py` | BW、SE | 是 | §6.1 |
| ToneCurve、ShadeCorrection | `sony_tonecurve.py`、`sony_shade.py` | 按设置 | 否 | §6.2、§6.3 |
| 整图解码 `decode(arw)` | `sony_decode.py` | — | — | §9 |
| Rust 加速核心（可选） | `rust/sonylook_core` | — | — | §9.4 |

不运行、也没有移植的阶段：TrinityLPF（需要存储类型 4）、YNR（dev+0x22c = 0）、`FUN_1000b83e8` 降噪任务（本机型条件不满足）、ZcTaskDemosaic（只用于 GRBG 传感器）。

**与 Edit 导出图的对照**（DSC03591，Edit 保存过的会话设置，ST 外观）：全图 7008×4672 的 8 位 MAE **0.125**（sRGB），AdobeRGB 为 **0.133**。10 块全分辨率裁块（含 4 块最亮区域）：ST 0.153、FL2 0.138、FL3 0.138。其余 9 种外观没有 Edit 参考图，只验证了与 Sony 函数逐位一致。

**仍未完成**：3DLut 表的构建链；自定义白平衡（模式 0xd）的数据来源；非 RGGB 传感器；更多参考图（其余外观、NR/DRO 开启、调过滑块、高 ISO）。

## 1. 管线顺序与插入条件

构建函数 `FUN_1000b85e0`（RAW 路径）按以下顺序插入任务（RTTI 类名 `sony_zhacai::ZcTask*`）。SIMD 和 OpenCL 变体的开关（`FUN_10004f5a8`、`FUN_10014a1bc`）在本版本恒为 0，运行的都是普通实现。

```
RawNR → ITP → [DRO/Vatr] → [GeoCorr] → SSCS
→ LinearMatrix16 → MainGamma → RGB2YCC → [AreaComp] → ChromaSuppress
→ Sharpness → YGamma → [3DLut] → [HueSaturation] → [YNR] → Spica
→ YCC2RGB → [Marble] → [Effect] → [ToneCurve] → [ShadeCorrection]
```

| 阶段 | 插入条件 |
| --- | --- |
| RawNR | dev+0x218（0x801e）= 1，且解码器 vtable+0x100 ≠ 0 |
| ITP | CFA 为 RGGB（look+0x84..0x90 = 0,1,1,2）；GRBG 时改用 ZcTaskDemosaic |
| DRO | dev+0x238（0x8022）开启，且存储类型 ≠ 4 |
| GeoCorr | look+0x12c0 ≠ 0（有 0x7982 表），且畸变开关为开（`FUN_1000aed68`） |
| SSCS | 无条件 |
| LinearMatrix16 | 有 0x780f（look+0x1c2 ≠ 0）；否则用旧的 0x7800 矩阵 |
| AreaComp | 格式码 ≥ 0x3233，`&0xf000 == 0x3000`，≠ 0x3fff（`0x1000b7b20`）。参数：格式码 > 0x33ff 且有 0x780f 时用新参数，否则用旧参数（`FUN_1000b3db8`，旗标 0） |
| 3DLut | dev+0x248（0x8026）≠ 0 |
| HueSaturation | dev+0x204 或 dev+0x1fc ≠ 0 |
| Spica | `srsettings0000.tbl` 能加载（在 Edit.app/Contents/Resources 中） |
| Marble | (dev+0x218 = 1 且 look+0x109c ≠ 0) 或 dev+0x2c0（清晰度）≠ 0 |
| Effect | look+0x34 & ~1 == 0x10（BW、Sepia） |
| ToneCurve | 4 条曲线中任意一条不是恒等 |
| ShadeCorrection | dev+0x17c 或 dev+0x180 ≠ 0（0x8031 / 0x8032） |

**外观编号**（look+0x34 = look+0x20，由外观名 0x7770 经 `FUN_10009ee90` 查表）：Standard 2、Vivid 4、Neutral 7、Portrait 8、BW 0x10、Sepia 0x11、FL 0x12、VV2 0x13、IN 0x14、SH 0x15、FL2 0x16、FL3 0x17。

**格式码**：解码器 vtable+0x58，由 0xb000 FileFormat 换算，A7M5（ARW 6.0）为 0x3600。

## 2. 前端：黑电平、缩放、白平衡

**没有相机矩阵**。有 0x780f 时，外观表直接作用在白平衡后的相机原生 RGB 上。

ITP 的缩放（vtable+0x60 = `FUN_1000f13bc`，float32）：

```
v = (look[0x40] · 2⁻¹⁵ · gain[cfa]) · 0.0625 · (raw − black[cfa])
```

- black = SR2 **0x7310**（512×4），look[0x40] = **0x7311**（256）。CFA 下标 `(x&1) | (y&1)<<1`。
- 结果截到 0..16383、转 u16。G 通道 ≈ 0.5·(raw − 512)·1.03125。

**白平衡增益** `FUN_10013c4b0`，按 dev+0x1e8（0x8002）分支：

| 模式 | 含义 | 增益来源 |
| --- | --- | --- |
| 1 | 拍摄时 | 0x7313 WB_RGGBLevels |
| 2 | 色温 | 0x8013 在 5 个节点 2500/3200/4500/6000/8500 K（0x782d/0x782c/0x7824/0x782b/0x782a）间插值（`FUN_10013bda0`），8500 K 以上外推 |
| 3 | 取色 | 解码器 +0xb0/+0xb4 的两个 float × 1024，由 `FUN_1000d3438` → `0x1000ae728` 算出 |
| 4–0xc | 预设光源 | 0x7820、0x7821、0x7825、0x7826、0x7828、0x7827、0x7829、0x7822、0x7823 |
| 0xd | 自定义 | look+0x42/+0x44/+0x48（来源标签未确认） |
| 其他 | — | 全部 1024 |

- **色调**（0x8014）：由 R 反推色温（`FUN_10013c0bc`），按所在节点段的差值乘色调后叠加（`FUN_10013c29c`）。**模式 2、3 跳过色调。**
- **曝光与 0x7809**：所有增益乘 `(0x7809 / 256) · exp2(EV / 300)` 后截断。0x7809 = 264（look+0x9d8，缺省 256），EV 为 0x8018（1/300 EV）。
- **R/B 系数**（0x8015）：R、B 再乘 `exp2(|v| · 0.01 · 0.678)`，v < 0 时用 `2 − 该值`。
- **取色**（`wb_pick_gains`）：在框内按 CFA 通道求 `raw − black` 的平均（float32 顺序累加，框小于 3 px 时外扩一圈）；G/R、G/B 截到 [0.1, 32]；亮度 `L = 0.3R + 0.1B + 0.6G` 决定权重（L < 300 为 L/300，300–7000 为 1，7000 以上线性下降）；再与拍摄时增益混合，截到 [0.2, 5]。
- **取色参数从保存文件读取**：0x9015 ≠ 0 时直接用 0x9016 的两枚 Q10 增益；否则用 0x8021 的前四个 short 作完整 RAW 坐标上的取色框。

验证：`verify_wb.py` 179 例、`verify_wb_pick.py` 11 例，全部逐位一致。节点和预设在 12 张 A7M5 ARW 上与 Sony 解析器一致。

## 3. RAW 域与线性域阶段

### 3.1 RawNR（`0x1000e9b48` → `0x1000e9d04`）

在 ITP 之前作用于含黑电平的 u16 CFA，四个通道各自在同色格点上处理。

- **参数**：SR2 **0x78c0–0x78d7** → look+0x1018–0x1098。0x78cc–0x78ce 截到 256，G 超出 256 的部分换算为 Sharpness 系数 look+0x1060 = 1 + (v − 256)·0.003125（本图 480 → **1.7**）。
- **噪声表**（`FUN_1000d5ac0`）：滑块 dev+0x21c/0x220/0x224（0x8027/0x8028/0x8029）。阈值 `T[i] = (g·3·L1034 >> 8) + ((clamp(i, L102c, L1030) − L102c)·(3·L1038·g >> 8) >> 12)`，g = L103c·32·(0.03125 + s·0.0003125)；混合 W = 512 + 5.12·s；高频增益 L1048，限幅 min(L1054, 8184)。
- **处理**：拆成高频 hp 和基底 b（R/B 与 G 的核不同）→ 5×5 同色格点（间隔 4）sigma 滤波 → `out = clamp(clamp(gain·hp >> 8, ±lim) − off + avg, 0, 0x3fff)` → `trunc(fmadd(out − raw, s, raw))`。s 按 ISO：< 400 为 0.4，≥ 1600 为 1.0，中间线性。

验证：噪声表 8 组滑块 × 3 通道、Process 9 个图块（29 336 像素），全部逐位一致。

### 3.2 ITP 去马赛克（`0x1000f95d0`）

边缘方向自适应：二阶差分和 5×5 邻域统计给出方向权重，先重建 G，再重建 R−G、B−G；输出截到 0..16383 的 u16，有效区每边内缩 10 px。插值保持局部均值。

- **整图**：1024×1024 不重叠分块（0x100139788、0x1000de588），每块多留 2 px（`halo=12`）后取中心。图像边界按 TileDiv 的奇偶规则延拓：左侧 `x & 1`，右侧 `(W−1) − ((W−1 ^ x) & 1)`。
- **常量**：0x1003b96b8 起的常量块由静态初始化函数 0x1000f9528 填写，文件里没有初值。

验证：全部叶函数、整条 Process（5 个真实 RAW 裁块 + 2 个随机 CFA）和跨分块整图，全部逐位一致。

### 3.3 DRO（ZcTaskVatr，`0x1000e65a4`）

局部影调算子，三通道乘同一增益。

- **分析**（`FUN_1000fc734`、`FUN_1000fd814`、`FUN_1000fda44`）：每个 RGGB 四元组算 `v = max(R−bR,0)·0.1495 + (max(G−bG,0) + max(B−bB,0))·0.057`，查表得 log2；16×15 采样取均值得 112×84 子格，14×14 子格为一格（8×6 格）；每格建 14 bin 直方图，3×3 十字平滑，再按 bin 模糊得到 W、S。
- **曲线**（`FUN_1000fe258`）：10 个节点 + 28 个 Bézier 控制值，采样 104 点。ARW 带有相机算好的 **0x781b / 0x781c** 时直接用，否则用内置 0/5/10 档表插值。
- **逐像素**：`L = log2(2·(0.1495r + 0.2935g + 0.057b))`；网格双线性 + bin 线性插值得 `local = S/W`；`gain = exp2(((L − local)·强度 − L) + 曲线(local))`；`out = trunc(clamp(v·gain, 0, 16383)) << 1`。

验证：网格 7 个中间数组 × 3 组输入、Vatr 12 个图块，全部逐位一致。

### 3.4 GeoCorr：畸变与横向色差（`0x100140b34`）

镜头结构由 `FUN_1000d58f0` 生成。

- **表**：G[i] = (float)(1 + 0x7984[i]·2⁻¹⁵)（19 个节点）；R、B = G·(1 + CA·2⁻²¹)，CA 取 0x7980 的前 16 / 后 16 个值。
- **几何**：中心 = 解码器矩形（vt+0x88，RAW SubIFD 0xc61f + 0xc620，全画幅 (12, 8, 7020, 4680)）的整数半宽高。节点间距 = 半对角线 / (k·0.69348127)，k = 21.63（全画幅，0x7983[1] = 16）或 14.2（APS-C）。镜头偏移由 0x78a2/0x78a3 计算。
- **缩放 s**：在角落查 G 表找到恰好填满画面的放大量（本图 1.019050）。
- **逐像素**：`r = sqrt(fma(dy,dy,dx·dx))`，按 `t = s·r/step` 线性插值得 f，`src = fma(f·((p+off)−c), s, c)`，FMA 链双线性采样后截断为 u16。
- **开关**：畸变 dev+0x278（0x8040）、色差 dev+0x27c（0x900d），经 `FUN_1000aed68/aed80`：−1（自动）和 1 为开，其余为关。
- **Edit 不做镜头暗角校正**：解析器不读 0x797d，阴影表 0x78a4–0x78ac 全为 0。

验证：镜头结构 12 组、GeoCorr 4 个区域，全部逐位一致。

### 3.5 SSCS（`0x1000f0e14`）

逐像素，在 GeoCorr 之后、LinearMatrix16 之前，把又亮又饱和的颜色向亮度收拢。

- **参数**：T = **0x7882**（本图 7934），起始阈值 tR/tG/tB = **0x7883/0x7884/0x7885**（2380）；CFA 权重 **0x78f7**（缺省 32×4，按 CFA 求和得 wR/wG/wB = 32/64/32）。
- **算法**（R > tR、G > tG、B > tB 同时成立才处理）：
  ```
  kG  = min(((G − tG) << 7) / (T − tG), 128)
  c   = min(R, B) 对应的通道；kRB = c > T ? 128 : ((c − tc) << 7) / (T − tc)
  f   = 128 − (kG·kRB >> 7)
  L   = wR·R + wG·G + wB·B；dR = f·(R − G)；dB = f·(B − G)
  G'  = ((L << 7) − dR·wR − dB·wB) >> 14
  R'  = ((G' << 7) + dR) >> 7；B' = ((G' << 7) + dB) >> 7
  ```
  加权亮度不变，色差按 f/128 缩小。浮点平面另有一条公式相同的 float32 路径（0x1000f10dc）。

验证：5 组参数 × 整数/浮点 × 3 次，30/30 逐位一致。约 5% 的像素会被改动。

## 4. 外观链

### 4.1 LinearMatrix16（`0x1000acdd4`，0x780f 的 1024×18 表）

输入为三平面 float32、14 位线性。

1. **低通**：每个平面水平 `[1 8 14 8 1]/32`，边缘有专门系数。
2. **色相**：`Y = (109R + 366G + 37B)/512`（double），a = R−Y、b = B−Y，按八分象限查 0x100234730 的 512 项 atan 表，`hue ∈ [0,1024)`。用低通后的平面计算。
3. **查表**：`i = min(trunc(hue), 1023)`，第 i 行前 9 个 float 为矩阵 B，后 9 个为 N。
4. **混合**（每通道）：`lin = B·(r,g,b)`（FMA 链），`q = N·(r²,g²,b²)/16384`；q ≥ thr 时 `out = lin + q − thr`，q ≤ −thr 时 `lin + q + thr`，否则 `lin`；截到 0..16383。本批外观 thr = 0，所以**平方项只在为正时叠加**。

**输出色彩空间**：`output_mode` 选静态矩阵，`param8` 表示工作色域。sRGB 输出为 mode 0 / param8 0（不乘矩阵），AdobeRGB 输出为 mode 1 / param8 0（乘 sRGB → AdobeRGB 线性矩阵 `0.7152 0.2848 0 / 0 1 0 / 0 0.0412 0.9588`）。外观表按 sRGB 原色编写。机型修正矩阵的数据库指针 `DAT_1002f7450` 从未被赋值，所以修正恒为单位矩阵。

### 4.2 MainGamma（构建 `0x1000c7774`，应用 `0x1000e96a0`）

0x7805 / 0x7806 各 128 个 int32（look+0x5d8/+0x7d8），x 的单位是 14 位线性 × 128。

```
for i in 1..32767:  y = 分段线性插值(0x7805 → 0x7806, x = i·128)    # float32，截断
    y = trunc(fma(frac/64, t[k+1] − t[k], t[k]))，k = y >> 8，frac = (y >> 2) − 64k   # 二级曲线
    LUT[i] = y >> 4
LUT[−m] = −LUT[m−1]
应用：out = trunc(fma(LUT[i+1] − LUT[i], v − floor(v), LUT[i]))，i = trunc(v)
```

**二级曲线**（`0x10011e174`，37 条曲线族）承载 Edit 的对比度、高光、阴影滑块：下半段（0–471）用第 `18 + 0.2·(contrast − shadow)` 条，上半段（471–1024）用第 `18 + 0.2·(contrast + highlight)` 条，参数在 0x1000d32d0 选取。滑块都为 0 时取第 18 条恒等曲线 64·i。

### 4.3 RGB2YCC 与 YCC2RGB（`0x1000ee0cc`、`0x1000fb500`）

外观的色彩差异主要在这里：四个色差象限各有自己的剪切（色相旋转）和增益（饱和度）。

**参数选择**（`FUN_1000ed9b4`，按 dev+0x1e8 模式）：A7M5 的混合权重 0x7847/0x7848 都是 `0 1024 0 0`，所以默认得到 **p = 0x7842 + 0x7844**（主 IFD 的 0x7841 正好等于这个和）。解析时（`FUN_1000a74bc`）增益项还要除以饱和度因子 `0x1002e6950[sat+9]`，饱和度 0 时为 1。

```
Y   = ((19·128)·R + (38·128)·G + (7·128)·B) >> 13          # 0x7801 = 19 38 7
rg  = R − G，bg = B − G                                     # float32
sh(v) = sign-extend9((v>>2) & 0x1ff) / 256；gn(v) = ((v>>3) & 0xff) / 64
Cb' = fma(sh(rg<0 ? p3 : p1), rg, bg)
Cr' = fma(sh(bg<0 ? p2 : p0), bg, rg)
Cr' = Cr'·0.5·gn(Cr'<0 ? p7 : p5)；Cb' = Cb'·0.5·gn(Cb'<0 ? p6 : p4)
dev+0x248 == 0 时 Cr'、Cb' 限制在 [−8192, 8191]
输出：Y，clamp_u16(trunc(Cr' + 32768))，clamp_u16(trunc(Cb' + 32768))
```

YCC2RGB 是固定的 BT.601 逆变换（1/10000 整数，向零截断，截到 0..16383）：`R = Y + 1.4020·Cr`，`G = Y − 0.7141·Cr − 0.3441·Cb`，`B = Y + 1.7720·Cb`。

`FUN_1000d5700` 还会构建一张 129×129 的表（render+0x49910），但没有任何代码读取它。

### 4.4 AreaComp（`0x1000b3aec`）

只修改色度平面，作用于中性灰附近的一个小楔形（最多移动 2 个单位）。用 4 条边界距离 `fmadd(c1+b, k1, (c0+a)·k0)`（负值截 0）取最小值 m，输出 `a + 32768 + (float)(m·k)`（double 中计算后截断）。区域常量见 `sony_ycc_stages.py`，Portrait 另有一组。

### 4.5 ChromaSuppress（`0x1000e6da0`）

在暗部和高光压低色度，12 种外观的参数相同。

- **阈值**：亮部阈值 0x787f[0] = 15360 定义在 **YGamma 输出域**。先经 YGamma 逆表（render+0x89962，`FUN_1000c2690`）和 MainGamma 逆表（render+0x69962，`FUN_1000c2618`）反推到线性，乘曝光系数 `render+0x18ee = (short)(exp2(EV/300)·64)`，再正向映射回来（`FUN_1000c2204`）。曝光为 0 时阈值为 15356。暗部阈值 = 亮部值·f0e/f06。
- **逐像素**：`f = clamp(255 − (Y 越界量·斜率 >> 12), 0, 255)`，色差按 `(C − 32768)·f/256`（向零截断）缩放，Y 的负值置 0。

### 4.6 YGamma（LUT `FUN_1000c7120`，逐像素 `0x1000feb68`）

- **LUT**：基础曲线（look+0x9dc，新路径下为恒等）→ 高光拐点：大于 `T = 曲线[0x780c >> 5]` 的部分改为 `T + trunc((0x780d/16384)·(v − T))`（ST 为 128/512、0.906，其他外观不起作用）→ 四段重采样 → 展开到 32768 点，负半轴置 0。
- **逐像素**：`v = LUT[Y]`；`v' = (v > s2 ? (v − s2)·512 : 0)·(1/(512 − dev2b0 + q)) − off`；`out = clamp(fma(v', gain, off), 0, 16383)`。s2、q 来自 BlackLevel dev+0x2ac（0x9018），dev2b0 是 WhiteLevel（0x9017）。默认 `out = gain·LUT[Y]`。
- **褪色**：级别 = dev+0x2bc（0x901b，= 10 × 褪色档位）。偏移 `FUN_1000d70e0`（主 IFD 0x780b = `0, 10624, …`）和增益 `FUN_1000d71fc`（外观 0x780e，10 项，值/16384）按 `q = level/10` 线性插值；**level > 90 时 q = 8 外推**，非整十的级别在两档之间插值。级别 0 时 ST 的增益为 17280/16384 = 1.0547。

**相机默认褪色**：FL2 = FL3 = 1、IN = 3、SH = 6，其余为 0。12 种外观的对比度、高光、阴影、饱和度默认都是 0。

### 4.7 HueSaturation（`0x1000e5840`）

用户的色相（0x8019 → dev+0x204）和饱和度（0x8016 → dev+0x1fc）滑块，不用外观数据。色相每档旋转 0.35°（负）或 0.7°（正），角度用多项式 atan2（`FUN_1000e53ac`，double，含 FMA）加 512 项 sin 表（`float(sin(i·2π/512))`）；饱和度为 `1 + 0.01·s`。位置在 YGamma 之后、Spica 之前。

### 4.8 3DLut（`0x10013db80`，默认不运行）

已移植**应用部分**：Cr/Cb 还原为差值轴并经过 1D shaper，按 Y、A、B 做 8 角整数权重（和为 512）的三线性插值，再变回 Cr/Cb。LUT 内容（render+0x49920，由 `FUN_1000d584c` → `FUN_1000d2c6c` 从 "beginning 3D-LUT" 构建）**没有分析**。

## 5. 空间处理（只作用于 Y 或 YCC）

### 5.1 Sharpness（`0x1000ef77c`，ChromaSuppress 之后、YGamma 之前）

区域先每边收缩 2。

- H：沿 x 做 7 抽头二项式 (1,6,15,20,15,6,1)；C：对 H 沿 y 再做一次（逐源像素 FMA 累加）。L = 4Y + 上 + 下 + 左 + 右。
- `d = (Y − L/8) + s9·(L/8 − C/4096)`；|d| ≤ thr 时置 0，超出部分乘 s0 / s1。
- `Y' = clamp(Y + k·d, 0, 32767)`。
- 参数（`sharpness_params`）：`thr = dev214 + 25`（dev214 < 0 时为 `(dev214 + 100)·0.25`），`s0/s1 = (dev20c/dev210 + 100)/100`，`s9 = 0.015·dev2c4 − 0.5`（dev2c4 > 50 时为 0.005·dev2c4），`k = look1060·0.02·(dev208 + 100)·(dev2c4·0.02)·s11`。dev208 = ApConGain（0x801a），dev20c/210/214 = OverShoot/UnderShoot/Threshold（0x801b–0x801d），dev2c4 = Fineness（0x901d，默认 50）。

### 5.2 Spica（`0x100100b60`，YGamma 之后、YCC2RGB 之前）

类 RAISR 的细节增强。表来自 `srsettings0000.tbl`（运行时从本机 Edit 读取）：256 个模式项（系数组号、dx、dy），每组 25 抽头，中心抽头 = 512 − 其余之和。

1. 十字 9 点普查得到 8 位类别（max − min < 8 归为第 0 类）。
2. 按类别取系数组和方向，做 25 抽头菱形滤波（FMA 链）。
3. `d = f − 512Y`，增益 = 2·min(g1(d/128), g2(max−min), g3((max+min)/2))（`FUN_1001006d4`）；`t = clamp(trunc(d·增益/512²) + Y, 0, 16383)`。
4. `Y' = Y + k·(t − Y)`，**k = k2·0.0002·(dev208 + 100)·(100 − dev2c4)**，默认 0.5。滤波区每边收缩 3，任务区收缩 4。

验证：Sharpness、Spica 各 8 例（默认参数），加 9 组滑块 × 两个任务 × 4 种输入共 72 例，全部逐位一致。

### 5.3 Marble（`0x100126f0c`，YCC2RGB 之后）

多尺度低频处理：降噪开时做色度降噪，清晰度非 0 时做亮度细节增强。

1. 转入 γ2.2 工作空间（与 Effect 相同的 `dd41c`），再转成 Marble 自己的 YCbCr：`Y = min((0xb44R + 0xdc3G + 0x271B) >> 11, 0xefff) + 0x1000`，Cb/Cr 偏置 0x8000，色度横向两两平均。
2. 区域宽高向上取整到 4 的倍数：右侧用第 x1 列补齐，**底部每一个补齐行**都复制最后一行。
3. **色度路径**（dev+0x218 = 1）：块平均（look+0x109e = 1 时 8×8，否则 4×4）→ 边缘各补 6 → 5×5（间隔 2）sigma 滤波（阈值随亮度变化，三个差值条件同时满足）→ 3×3 二项平滑 → 双线性放大回半宽 → 在偏红且 Cb 中性的区域把 Cr 拉回原值。
4. **亮度路径**（dev+0x2c0 ≠ 0）：8×8 块平均 → 5×5 sigma（阈值 look+0x1174）→ 3×3 平滑 → 8 倍双线性放大 → 把 Y − 放大值按亮度相关权重加回，**输出量化到 10 位**。
5. **强度**：`q = lum/10`、`r10 = lum%10`，在 11 档表上插值。有 0x7815 时（look+0x113e = 1）用 look+0x1140/+0x1154（0x7815/0x7816），否则用 `LUMA_STRENGTH`（0x1002ed728）× ISO 系数（< 3200 为 100；3200–409600 分段线性；以上为 50）。解码器 +0x22780/+0x22784 ≠ 1.0 时改走 `FUN_100129470`。
6. 色度复制回全宽，**有效区每边收缩 16**，按输出空间转回 RGB。收缩掉的边框保留 Marble 之前的值。

只要 Marble 运行，色度横向分辨率就减半，亮度量化到 10 位。

**参数**：0x794a → look+0x109c，0x794b–0x795d → +0x10a0–0x10c4（格式码 ≥ 0x3220）；0x795e → +0x109e（≥ 0x3230）；0x7810–0x781a → +0x1140–0x1176，0x7815 存在时 +0x113e = 1。12 种外观的这些标签相同。

验证：209 例全部逐位一致，覆盖降噪开关、清晰度 0–100、NR 强度、8×8 分支、ISO 分支、f0 = 1 和 +0x22780 ≠ 1.0。

## 6. 后段

### 6.1 Effect：BW / Sepia（`0x10013a9b8`）

参数 `+0x44` 是输出色彩空间（0 sRGB、1 AdobeRGB）。所有平面为 0..16383 u16。

1. **转入工作空间**：mode 0 用 `FUN_1000dd41c`（sRGB 解码表 → Q13 矩阵 → γ = 2.19921875 编码表），mode 1 用 `dd7cc`。四张表由 `FUN_1000db9f4` 用 `powf` 生成。
2. **BW**：`R = G = B = min((2449R + 4809G + 934B) >> 13, 16383)`，**不转回**，γ2.2 编码直接作为输出。
3. **Sepia**：`L = clamp(fmadd(B, 0.0722, fmadd(G, 0.7152, R·0.2126)), 0, 16383)`（double）；色调 (tR, tB) 在 Y < 1344 时乘 `Y·0.00074404763`，在 `s ≥ 0x7482000` 时乘 `(16320 − Y)/1408`；`R = trunc(tR + L)`，`B = trunc(tB + L)`，`G = trunc(L) − ((trunc(tR)·0x104c + trunc(tB)·0x636) >> 13)`；再用 `dd5f4`（mode 0）或 `dd9a4`（mode 1）转回输出空间。
4. **色调来源**：tR = look+0x9a << 6，tB = look+0x98 << 6，来自 SR2 **0x74c5**（B, R）。SonyModelID < 3 或属于 {0x100, 0x102..0x105} 时改用内置 (832, −1152)。A7M5（ID 407）：0x74c5 = −15, 14 → tR = +896、tB = −960。

验证：32 例逐位一致（BW/SE、mode 0/1、多个机型和色调）。

### 6.2 ToneCurve（`0x1000fae94`）

曲线工具的 4 条曲线，设置 0x9000+k（x）、0x9004+k（y），0..511 对应 0..1，处理顺序为 **R、G、B、Y**。

- **控制点**：先放 (0,0)、(1,1)；x = 0 和 x = 511 的点替换端点，其余按 x 插入；删除设置里没有显式给出的默认端点。
- **曲线**（`FUN_1000c3a0c`）：相邻控制点间为三次 Bézier，切线 `v = (next−cur)·(li/lo) − (prev−cur)·(lo/li)`，控制柄 `−(li/3)·v/|v|` 和 `+(lo/3)·v/|v|`（限制到相邻 Δx），端点控制柄为 0。`FUN_1000c4b80` 每段采样 32 点，线性填入 65536 项 u16 LUT（y·65535）。
- **处理**：`Y = (R·0x991 + G·0x12c9 + B·0x3a6) >> 11`（4×亮度）查 Y 曲线，用 C1/C2 重建 R/G/B 后分别查表，输出 `(v >> 2) & 0x3fff`。

### 6.3 ShadeCorrection（`0x1000eec2c`）

- **系数**（`FUN_1000c4ee8`）：0x8030 PeripheralIllumination（默认 80）、0x8031 CentralRadius、0x8032 CentralStrength，生成 11 个径向增益：前段 `cos(radius·0.0007853982·i)⁴`，后段按 strength 与中心值混合。
- **增益**（`FUN_1000c5410`）：归一化半径 r，`i = trunc(10r)` 线性插值，r ≥ 1 取最后一项。0x900e = 1 时按 0x900f（0.001°）和图像矩形做投影缩放（`FUN_1000aedb8`）。
- **处理**：`Y' = clamp(trunc(Y/gain), 0, 65535)`，用 C1/C2 重建，右移 15、截到 0..0x3fff。gain == 1 的像素原样保留。

验证：ToneCurve 曲线 8 组、处理 2 组；ShadeCorrection 系数 5 组、投影 6 组、处理 3 组，全部逐位一致。

## 7. 显影设置

### 7.1 存储位置

Edit 保存 ARW 时在文件末尾追加一个小 TIFF 块（4.0.00.10312 为 1630 字节）：`0xd000` 版本 1，`0xd001` 指向两个 IFD 偏移（相对块起点）。

- **两个 IFD 是两个快照**：第一个是拍摄时（0xd100 为拍摄时间），第二个是 Edit 会话。**导出用的是第二个。**
- 条目为 `{设置 ID, 类型, count, 值}`，类型 3 = SHORT、4 = LONG、9 = SLONG；count > 1 时值在**该 IFD 起点**的相对偏移处。
- `FUN_1000aac90` 把设置逐项复制到 dev 结构，**不做限幅**；消费者按 int 读取，所以 LONG 0xFFFFFFFB 就是 −5。

`develop_settings.read_settings(arw)` 返回 Edit 会话快照，未保存的 ARW 返回 `{}`；`effective_settings(arw)` 在后一种情况下返回 §7.3 的初始值。

### 7.2 设置 ID

| 设置 | dev | 名称 | 消费者 |
| --- | --- | --- | --- |
| 0x8000 | +0x1e0 | 外观选择 | 1 = 拍摄时的外观（0x7770），否则为外观编号（vtable+0xe0） |
| 0x8002 | +0x1e8 | 白平衡模式 | §2；同时是 RGB2YCC 的参数模式 |
| 0x8013 / 0x8014 / 0x8015 | +0x1ec / +0x1f0 / +0x1f4 | 色温 / 色调 / R/B 系数 | §2 |
| 0x8016 | +0x1fc | Saturation | HueSaturation |
| 0x8017 | +0x200 | Contrast | MainGamma 二级曲线 |
| 0x8018 | +0x1f8 | Exposure（1/300 EV） | 白平衡增益、ChromaSuppress 阈值 |
| 0x8019 | +0x204 | Hue | HueSaturation |
| 0x801a–0x801d | +0x208–+0x214 | ApConGain / OverShoot / UnderShoot / Threshold | Sharpness；ApConGain 也进 Spica |
| 0x801e | +0x218 | 降噪 | RawNR、Marble 色度路径 |
| 0x8021 | +0x24c… | 取色框（8 个 short） | 取色白平衡 |
| 0x8022 / 0x8023 | +0x238 / +0x23c | DRO 开关 / 等级 | DRO |
| 0x8026 | +0x248 | 3DLut | 3DLut；同时关闭 RGB2YCC 限幅 |
| 0x8027–0x8029 | +0x21c–+0x224 | 降噪滑块 | RawNR 噪声表；0x8029 也是 Marble 强度 |
| 0x8030–0x8032 | +0x178–+0x180 | 周边光量 / 中心半径 / 中心强度 | ShadeCorrection |
| 0x8040 | +0x278 | 畸变校正 | GeoCorr（−1 自动、1 开、其余关） |
| 0x9000–0x9007 | +0x10/+0x68/+0xc0/+0x118 | 曲线工具 x / y 控制点 | ToneCurve |
| 0x900d | +0x27c | 色差校正 | GeoCorr |
| 0x900e / 0x900f | +0x280 / +0x284 | 投影开关 / 角度 | ShadeCorrection |
| 0x9015 / 0x9016 | +0x26c / +0x270 | 取色：使用预计算增益 / 增益（Q10） | 取色白平衡 |
| 0x9017 / 0x9018 | +0x2b0 / +0x2ac | WhiteLevel / BlackLevel | YGamma |
| 0x9019 / 0x901a | +0x2b4 / +0x2b8 | HighlightLevel / ShadowLevel | MainGamma 二级曲线 |
| 0x901b | +0x2bc | Fade（10 × 档位） | YGamma |
| 0x901c | +0x2c0 | Clarity | Marble 亮度路径 |
| 0x901d | +0x2c4 | Fineness | Sharpness、Spica |

参数名来自 Edit 自己的键名表（0x10029acac 起）。

### 7.3 未保存 ARW 的初始值（`FUN_1000a6e9c`）

Edit 首次打开文件时调用，`camera_defaults(arw)` 按汇编还原：

| 设置 | 初始值 |
| --- | --- |
| 0x8000、0x8002 | 1（拍摄时） |
| 0x801e 降噪 | SR2 0x78a0 ≠ 0，否则 0x78a1 ≠ 0 |
| 0x8022 / 0x8023 DRO | 厂商注释 0xb025：2、3（自动）→ 1；0 → 0；其他 → 2，等级 15·v − 215（A7M5 格式码分支） |
| 0x8040 畸变 | SR2 0x7983（为 −1 时用 0x7981）：u16 低字节 0x11 → −1，0 → 2，其他 → 1；≥ 0x100 → 0；没有 0x7982 表时为 0 |
| 0x900d 色差 | SR2 0x797f，规则同上 |
| 0x901c 清晰度、0x901b 褪色 | Edit 静态表 0x1002e8214（vtable+0x178，按外观编号，×10） |
| 0x901d Fineness | 50 |

- **清晰度默认**：ST、VV、NT、BW、SE、FL、IN、SH 为 1，PT 为 0，VV2 为 8（即 0x901c = 10 / 0 / 80）；IN、SH 的褪色为 3、6。**表里没有 FL2、FL3**，查表失败，所有默认值都是 0。
- DSC03591 原始文件算出的初始值与 Edit 写入的相机快照逐项相同。

### 7.4 越界值

Edit 的界面会限幅（例如 `GfDocument::ModifyClarity` 把清晰度限制在 0–100），但从保存文件读回时不会。本实现的规则是：**Edit 结果确定的值照算，Edit 读到表外的值拒绝**。

- **清晰度**：0–109 照算（11 档表的可寻址范围）；负数和 ≥ 110 抛 `ValueError`。Rust 对这些值返回 fallback。
- **褪色**：所有 ≥ 0 的级别照算（插值、外推）；负数抛 `ValueError`。

## 8. 解码器参数（ZcARW）

`decoder_params.py` 直接从 ARW 读出，对应关系按 `FUN_1000a107c` 的汇编还原：

| 字段 | 来源 | 用途 |
| --- | --- | --- |
| +0x148 存储类型 | RAW SubIFD 0x115 SamplesPerPixel | vtable+0xf0 = (存储类型 == 4)，即 Marble 的 f0 |
| +0x14c 采样格式 | RAW SubIFD 0x153，缺省 1 | Marble 强度分支 |
| +0x1f8 ISO | Exif 0x8827；缺失或 0xffff 时按 SensitivityType（0x8830）选 0x8831/0x8832/0x8833 | Marble、RawNR、Spica 的 ISO 分段 |
| +0x19c…+0x1bc | 厂商注释 0x2004–0x2006、0x2032–0x2036，经 `FUN_1000a2b74` 和 5 张小查找表 | +0x1b0 是 `FUN_100129470` 的档位上限 |
| +0x22780 / +0x22784 | 构造函数缺省 1.0，ARW 路径不改写 | Marble 强度系数 |
| 解码器矩形（vt+0x88） | RAW SubIFD 0xc61f + 0xc620 | GeoCorr 的中心和节点间距；输出尺寸 |

验证：12 张 A7M5 ARW（ISO 100–204800）与 Sony 解析器的结果全部一致。

## 9. 整图解码

### 9.1 流程（`sony_decode.decode`）

```
RAW → [RawNR] → ITP → [DRO] → [GeoCorr] → SSCS → LinearMatrix16 → MainGamma
    → RGB2YCC → AreaComp → ChromaSuppress → Sharpness → YGamma → [HueSaturation]
    → Spica → YCC2RGB → [Marble] → [Effect] → [ToneCurve] → [ShadeCorrection]
```

- **设置**：`effective_settings(arw)`（§7）。外观、各阶段开关和所有滑块都从这里来，显式参数可覆盖。
- **输出**：解码器矩形，A7M5 为 7008×4672，uint16 0..16383（14 位编码）。
- **边缘**：空间阶段在解码矩形外多取 32 px 真实 RAW（`pad=32`）上运行，最后裁回。
- **分条**：外观链按 512 行一条处理，每条在 [y−8, y+8) 上跑逐像素部分，Spica 用精确的 [y−3, y+3) 子条；Marble 按行分块，上下各留 12 个块的上下文；DRO 按 256 行、GeoCorr 按 512 行。结果与整图处理逐位一致。

### 9.2 输出色彩空间与文件格式（`sony_export.py`）

- **色彩空间**：`output="srgb" | "adobe"`。同一个 render+0x44 同时传给 LinearMatrix16、Marble 和 Effect。
- **位深**：Edit 的 16 位 TIFF 是 14 位值 << 2（最大 65532）；8 位是 14 位值 >> 6，直接截断。
- **ICC**：Edit 只在 AdobeRGB 时嵌入 `Sony_AdobeRGB_1998.icc`（EXIF ColorSpace = 0xFFFF），sRGB 导出不嵌（ColorSpace = 1）。本实现两种都嵌，配置文件运行时从本机 Edit 安装目录读取。

### 9.3 性能（Apple Silicon，DSC03591）

| 后端 | 全流程 |
| --- | --- |
| NumPy | 约 45 s（ITP 6、GeoCorr 8、外观链与空间处理约 30 s） |
| Rust（`--rust`） | 约 3.5 s |

RawNR、DRO 和 Marble 的色度路径目前只有 NumPy 实现，打开时各增加约 5–9 s。

### 9.4 Rust 核心（`rust/sonylook_core`）

C ABI 动态库，由 `sonylook/fused_rust.py` 通过 ctypes 加载（`SONYLOOK_RUST=1` 或 `rust=True`）。覆盖 ITP、GeoCorr、SSCS 整数路径、LinearMatrix16 → ChromaSuppress、Sharpness → YCC2RGB、Marble 亮度路径，以及 MainGamma、Effect 表的构建。

- NumPy 是参考实现，Rust 必须与之逐位一致：float32 保持原求值顺序，FMA 用 `f32::mul_add`。
- 动态库缺失或缺符号时退回 NumPy（只尝试加载一次）。表长度不足时在调用 Rust 之前报错。
- 空间、SSCS、Marble 的参数块每次 `render()` 重建，只有只依赖外观的前端参数块被缓存。
- 验证：DSC03591（sRGB / AdobeRGB）和 DSC05740（BW、NR、DRO 开启）整图与 NumPy 逐位一致；Marble 62 例（含高度非 4 的倍数、无 0x7815 的外观、各 ISO 分支）；滑块与参数复用场景。

## 10. 与 Edit 成片和其他版本的对照

### 10.1 与 Edit 导出图

- DSC03591 的 Edit 会话设置为：NR、DRO、色差校正关，清晰度 10，其他滑块默认。用这份设置解码，ST 全图 MAE 0.125（sRGB）/ 0.133（AdobeRGB）；改用相机快照（NR、DRO、CA 开）则明显变差，这也证实了导出用的是第二个快照。
- FL2、FL3 的参考图是 Edit 用其默认值（褪色 0、清晰度 0，Marble 不运行）导出的；与相机直出的默认值（褪色 1）不同。
- 剩余误差集中在边缘和细纹理，量级在 1 个 8 位色阶以内。

### 10.2 Edit 4.1.00.03062

4.0 的 10021 个函数中有 9683 个在 4.1 里完全相同。本文引用的函数中，改动只涉及结构体偏移（解码器对象增大约 0x888 字节）、ARW 解析器的新机型支持、DRO 内置强度表扩展到 0–150（Lv6–8；ARW 自带 0x781b/0x781c 时不用这段），以及取色白平衡新增的两个辅助函数（未验证）。外观结构、默认参数表、白平衡主函数和全部颜色阶段与 4.0 相同。

## 11. 与配置文件（Capture One / Camera Raw）有关的结论

- **进入配置文件**的是逐像素阶段：SSCS、LinearMatrix16、MainGamma、RGB2YCC、AreaComp、ChromaSuppress、YGamma、YCC2RGB、Effect。前端（白平衡、去马赛克）由宿主软件完成；空间阶段（RawNR、DRO、GeoCorr、Sharpness、Spica、Marble）不进入。
- 配置文件按相机默认参数渲染：褪色 FL2/FL3 = 1、IN = 3、SH = 6，其余滑块为 0。
- **SSCS**：在两家实际导出上都降低了亮区色度误差（Capture One 0.64 → 0.16，Camera Raw 0.61 → 0.33），从 v1.1.0 起包含。
- **Camera Raw 亮区偏亮约 0.5–0.7 级**：来自 Camera Raw 按画面内容自适应的高光压缩，固定的 RGBTable 无法修正。
- 宿主软件各自的标定见 [capture_one.md](capture_one.md) 和 [camera_raw.md](camera_raw.md)。
