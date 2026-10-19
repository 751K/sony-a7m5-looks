# Sony A7M5 创意外观 → Capture One / Camera Raw / DxO PhotoLab

[English](README.md) | 中文

天下苦创意风格久矣，CameraRaw 内迟迟没有上线A7M5的创意风格文件

本仓库通过逆向Sony Image Edit，把 Sony α7 V（ILCE-7M5）的 12 种 Creative Look移植成 Capture One、Camera Raw / Lightroom 和 DxO PhotoLab 的配置，并通过对比检验，色度上基本没有差异。

![Edit / Capture One / Camera Raw 对照](images/compare_ST.jpg)

## 效果

用同一张 ARW 和 Edit 导出图对比（DSC03591，几何配准后缩小 8 倍，8 位平均误差）：

| 外观  | Capture One | Camera Raw | 参考：外观链本身 |
| --- | ----------- | ---------- | -------- |
| ST  | 1.17        | 1.27       | 0.90     |
| FL2 | 1.05        | 1.15       | 0.89     |
| FL3 | 1.21        | 1.27       | 1.00     |

- 平坦区色差 ΔE₀₀ 约 0.5。整体偏色 ΔE₀₀ < 0.4，色度差 ΔC* 在 C1 上为 −0.28，在 ACR 上为 −0.01，肉眼看不出来。
- 剩下的差异集中在边缘和细纹理，来自各软件自己的去马赛克和锐化，不属于颜色风格。
- 合成色卡上，配置与 Sony 外观目标的差异：C1 为 0.10–0.15，ACR（64³）为 0.14。
- 亮而饱和的颜色（v1.1.0）：配置文件现在包含 Sony 的 SSCS，它会把亮而饱和的颜色向亮度收拢。在 DSC03591 受它影响的约 5% 画面上，ST 与 Edit 的色度误差在 C1 中从 0.64 降到 0.16，在 ACR 中从 0.61 降到 0.33。
- DxO PhotoLab（v1.2.0）：用 PhotoLab 10.1 导出 DSC03591 的 ST（底座 DCP + 外观 LUT，拍摄时白平衡），和同一白平衡下的外观链渲染相比误差为 1.18，各通道整体偏差 0.25–0.67。还没有和 Edit 的导出图对照过。过曝前约 1.7 档以下，PhotoLab 线性地把颜色送进 LUT（色卡误差 0.11）；再往上由它自己的高光处理接管，那里的彩色色块平均差 4.6，最大 26。
- 覆盖范围：只有 ST、FL2、FL3 有 Edit 导出图可以对照。其余 9 种外观走同一套已验证的代码，只是数据不同，但没有和 Sony 的成片核对过。上表 FL2/FL3 的数字是在褪色为 0 时测的，Edit 渲染这两种外观时用的就是 0。从 v1.0.2 起，配置文件按相机默认的褪色生成（FL2/FL3 为 1，IN 为 3，SH 为 6；Edit 漏了 FL2/FL3 的这个默认值）。这一改动还没有和相机直出 JPEG 对照过。

## 安装使用

### Capture One

1. 把 `profiles/capture_one/SonyA7M5-Look-*.icc` 复制到 `~/Library/ColorSync/Profiles/`，然后**重启** Capture One。C1 会缓存已加载的配置。
2. 在 Base Characteristics 中，ICC Profile 选 `SonyA7M5 Look XX`，**Curve 选 Linear Response**。

### Camera Raw / Lightroom

1. 把 `profiles/camera_raw/SonyA7M5 Linear Base.dcp` 复制到 `~/Library/Application Support/Adobe/CameraRaw/CameraProfiles/`。
2. 把 `profiles/camera_raw/SonyA7M5 Look *.xmp` 复制到 `~/Library/Application Support/Adobe/CameraRaw/Settings/`。
3. 重启 Photoshop / Lightroom，在配置文件浏览器的 **Sony A7M5** 组里选择外观。

### DxO PhotoLab

需要 PhotoLab **Elite**（用到 *LUT Grading* 面板），在 macOS 的 PhotoLab 10.1 上测试过。PhotoLab 按路径引用 DCP 和 LUT 文件，所以要把它们放在一个不会删的文件夹里。

1. 把 `profiles/dxo/SonyA7M5 Linear Base v2.dcp` 和 `profiles/dxo/SonyA7M5 Look *.cube` 复制到这个文件夹。
2. 先套用 **6 - 无校正** 预设，或者确认 Smart Lighting、ClearView、对比度都已关闭。镜头校正（畸变、色差、镜头锐度）可以再打开，它们不影响外观的颜色。
3. **颜色渲染**（Color/B&W Rendering）：类型选 **DCP 配置文件** → *导入 DCP 配置文件…* → `SonyA7M5 Linear Base v2.dcp`；**DCP 曲线选 Adobe**，强度 100，**饱和色保护设为 0**（关掉自动）。
4. **LUT（查找表）**（DxO PL Elite – LUT Grading）：LUT 文件 → *导入（.cube）…* → `SonyA7M5 Look XX.cube`；**LUT 色彩空间选 ProPhoto RGB**，强度 100。
5. 可以把这套设置存成预设，换外观时只换 `.cube`。
6. 想和 Sony 原片完全一致的话，**暗角**校正保持关闭。Edit 不补偿暗角，打开后四角会偏亮，和 Camera Raw 的情况一样。

### 适用范围

- 仅适用于 A7M5 的 RAW。
- 对于其他机型，存在一定的色彩差异（但是在肉眼容差范围内）

## 原理

### 概述

Sony 创意外观可以视为一个从**白平衡后的相机原生 RGB** 到**显示编码 sRGB** 的确定性色彩映射 *f*。本项目分三步完成移植：

1. 复现 *f* 的计算过程；
2. 把 *f* 离散化为三维查找表（3D LUT）；
3. 标定目标软件在配置文件之前对输入施加的编码 *E*，并在查找表中加以补偿。

最终写入配置文件的映射为 *f* ∘ *E*⁻¹。目标软件在处理时先施加 *E*，再查表，两者复合后恰好得到 *f*。

```
相机 RGB ──E（目标软件的输入编码）──▶ 配置文件：f ∘ E⁻¹ ──▶ 输出颜色 = f(相机 RGB)
```

### 1. 外观映射 *f* 的复现

本项目对 Imaging Edge Edit 4.0 进行了静态分析，确定外观链由以下逐像素阶段依次构成：

1. **LinearMatrix16**：以色相为索引（1024 档）选取 3×3 矩阵并附加二次项。它同时承担相机色彩校正和外观的色彩倾向。
2. **MainGamma**：逐通道的一维影调曲线。
3. **RGB2YCC**：转换到亮度–色差空间，在色差平面的四个象限内分别施加剪切与增益，相当于分区域调整色相和饱和度。
4. **ChromaSuppress / AreaComp**：抑制极暗和极亮区域的色度，并对中性附近做微调。
5. **YGamma**：亮度曲线，含高光拐点与整体增益。外观的"褪色"在这里起作用：只要大于 0，就给暗部加一个固定的抬升，并逐档降低增益。
6. **YCC2RGB**：BT.601 逆变换。
7. **Effect**（仅 BW、SE）：转为单色，SE 再加棕褐色调。

另有几点：

- 前端只做黑电平扣除和白平衡增益，不含相机矩阵。
- 各外观的参数由相机写入 ARW 的加密 SR2 数据中，12 种外观共用同一实现。
- 本项目的 Python 实现（`sonylook/`）与原程序逐位一致，验证方法是在仿真环境中对照原始 ARM64 指令的输出。
- 每种外观都按相机里创意外观的默认参数渲染：12 种外观的对比度、高光、阴影、饱和度都是 0；褪色 FL2/FL3 为 1，IN 为 3，SH 为 6。
- 锐化、降噪、清晰度、DRO 等阶段依赖邻域像素，不属于逐像素映射，不纳入配置。
- 解码器的去马赛克（ZcTaskITP）已逐位移植到 `sonylook/sony_itp.py`，包括 Sony 的 1024 像素分块，不进入配置文件。
- 完整解码器已串联在 `sonylook/sony_decode.py`：`decode(arw)` 依次运行 RawNR、ITP、DRO、镜头几何、SSCS、外观链和 Marble；DSC03591 的全图 7008×4672 与 Edit 导出的 8 位 MAE 为 0.13。加 `--rust` 会走可选的 Rust 内核，输出逐位相同。
- 配置文件也包含 SSCS（`sonylook/sony_sscs.py`）。严格来说它属于外观之前的解码器阶段，但它是逐像素的，在两个软件里都能改善结果（见上文）。Edit 的 3D LUT 在默认参数下不运行；色相/饱和度只在对应滑块非 0 时运行。曲线工具（Y/R/G/B）和周辺光量补偿已逐位移植（`sonylook/sony_tonecurve.py`、`sony_shade.py`），在参数非默认时接在链路末尾。

### 2. 查找表表示

由于 *f* 是逐像素映射，可以在输入空间的规则网格上预先求值，用插值近似任意输入。

- Capture One 版本：输入 ICC 配置的 A2B0 表，65³ 网格，配合平方根形状曲线，以提高暗部的采样密度。
- Camera Raw 版本：XMP 配置中的 RGBTable，64³ 网格。
- DxO PhotoLab 版本：64³ 的 `.cube` LUT，按 ProPhoto RGB（γ1.8）读取。

在测试照片上，插值误差约为 0.2–0.5（8 位单位）。

### 3. 目标软件输入编码 *E* 的标定

目标软件传递给配置文件的并非线性相机 RGB，而是经过其内部编码 *E* 的值。*E* 未见文档说明，因此通过实验测定：

- **合成色卡**：改写 ARW 的 raw 数据，生成 264 个色块（48 级灰阶，加上相机 RGB 空间中的 6³ 立方体）。每个色块的线性值由 Sony 前端增益反算得到，精确已知。
- **探针配置**：
  - 恒等探针：直接输出其输入，用于测定 *E*；
  - 平方探针与亮度探针：检验配置之后是否还存在其他色调或色彩处理。

测定结果：

|           | Capture One（Linear Response 曲线）         | Camera Raw（线性底座 DCP）                    |
| --------- | --------------------------------------- | --------------------------------------- |
| *E* 的形式   | 三通道相互独立的同一条一维曲线：暗部线性，高光逐渐压缩；传感器饱和点映射为 1 | 中间调线性；高光压缩保持色相：最大、最小通道经同一条曲线，中间通道保持线性比例 |
| 配置之后的处理   | 无（平方探针误差 0.04，亮度探针误差 0.02）              | 无（RGBTable 为最后一步）                       |
| 模型在色卡上的残差 | 约 0.2                                   | 平均 0.04，最大 0.37                         |

DxO PhotoLab（底座 DCP `SonyA7M5 Linear Base v2`，DCP 曲线 = Adobe，LUT 在 ProPhoto RGB 中）：

- **中间调**：LUT 收到的是白平衡后的相机 RGB 乘以底座 DCP 的矩阵，线性，Sony 线性值 6422 对应 1.0（和 Camera Raw 的刻度相同）。
- **高光**：从过曝前约 1.7 档起，PhotoLab 的高光恢复会压缩较亮的通道，之后还有一段来源未定位的压缩；这部分用灰阶上实测的曲线建模。
- **高饱和色**：PhotoLab 在 DxO Wide Gamut 工作空间里把负分量裁成 0，所以很饱和的绿色、蓝色里较弱的通道会被抬起。
- **LUT 之后**：没有处理（灰阶上平方探针吻合到 0.03，亮度探针 0.33）。
- **模型在色卡上的残差**：高光门槛以下 0.11，以上 4.6（全部在彩色色块）。

两点补充：

- Capture One 的 *E* 依赖所选影调曲线，配置只在 Linear Response 下有效。
- Camera Raw 的底座 DCP 将 ForwardMatrix 设为线性 ProPhoto → XYZ D50，影调曲线设为恒等，使 Camera Raw 的工作空间与白平衡后的相机 RGB 一致，避免叠加 Adobe 自身的色彩和影调。

处理管线的最终结论见 [docs/pipeline.md](docs/pipeline.md)（推导过程在它的 git 历史里），各宿主软件的标定见 [docs/capture_one.md](docs/capture_one.md)、[docs/camera_raw.md](docs/camera_raw.md)、[docs/dxo.md](docs/dxo.md)。

## 重新生成

需要：Python 3.11+、`pip install -r requirements.txt`、任意一张 A7M5 的 ARW，以及本机安装的 **Imaging Edge Edit 4.0.00.10312**（macOS）。可选：Rust 工具链（`cargo`），用于加速的 `--rust` 解码；NumPy 路径不需要 Rust。一次性 LUT 会缓存在 `~/.cache/sonylook`（`SONYLOOK_CACHE=0` 关闭，`SONYLOOK_CACHE_DIR` 改目录）。

外观链用到的一些常量表（色相 atan 表、对比度曲线族等）是在运行时从你本机安装的 Edit 里读取的（`sonylook/edit_binary.py`），本仓库不包含任何 Sony 的二进制或数据。脚本会校验 arm64 部分的 SHA-256；其他版本的表地址不同，无法使用。路径可以用 `SONY_EDIT_BINARY` 指定。

```bash
python3 capture_one/c1_look_profile.py /path/to/A7M5.ARW
```

```bash
python3 camera_raw/acr_look.py /path/to/A7M5.ARW --write
```

```bash
python3 dxo/dxo_look.py /path/to/A7M5.ARW --write
```

```bash
python3 sonylook/sony_render.py /path/to/A7M5.ARW Standard FL2 --scale 4
```

- 第一条生成 C1 配置，第二条生成 ACR 配置，第三条生成 DxO 的 LUT（加 `--eval` 会打印在该 ARW 上的插值误差），第四条用外观链直接渲染预览。
- 用 DSC03591 重新生成的结果，与 `profiles/` 里的文件逐字节相同。

### 解码 ARW

完整解码器会从 RawNR 一路运行到创意外观：

```bash
python3 sonylook/sony_decode.py /path/to/A7M5.ARW --out out.tif
```

输出 7008×4672 的解码器矩形，可选 16 位 TIFF 或 8 位 JPEG / PNG，色彩空间 sRGB 或 Adobe RGB（`--color-space srgb|adobe`、`--bits 8|16`），并嵌入你本机 Edit 自带的 ICC 配置。外观、可选阶段、Edit 的 Creative Look 滑块（对比度、高光、阴影、色相、饱和度、黑白位、锐度精细度和曝光）、曲线工具（Y/R/G/B）和周辺光量补偿按 ARW 里 Edit 保存的显影设置决定；没保存过的文件，按 Edit 首次打开时的初始值（拍摄时的外观、相机的降噪和 DRO、镜头校正开、该外观的默认清晰度、滑块为 0）；`--no-nr`、`--no-dro`、`--no-marble`、`--no-detail`、`--no-lens`、`--no-sscs` 可分别关闭。

可选的 Rust 内核（`rust/sonylook_core`）把渲染 band 中的逐像素部分、Sharpness / YGamma / Spica / YCC2RGB 融合在一起，并移植了 GeoCorr、ITP 去马赛克、Marble 和 SSCS。用 `cargo build --release --manifest-path rust/sonylook_core/Cargo.toml` 构建，然后加 `--rust`。输出与 NumPy 路径逐位相同；测试图上融合阶段从 21 s 降到 0.5 s，GeoCorr 从 6.9 s 降到 0.5 s，ITP 从 5.8 s 降到 0.6 s，Marble 从 5 s 降到 0.5 s，SSCS 从 1.4 s 降到 0.1 s，完整 decode 从 42 s 降到 3.2 s。NumPy 仍是默认后端和参考实现；开启降噪时 Marble 的 chroma 分支仍走 NumPy。一次性 LUT 构造也做了优化：MainGamma 和 Effect 表在 Rust 里构建，LinearMatrix16/YGamma 表缓存在 `~/.cache/sonylook`（`SONYLOOK_CACHE=0` 关闭，`SONYLOOK_CACHE_DIR` 改目录）。线程数由 `SONYLOOK_THREADS`、`SONYLOOK_BAND_THREADS`、`SONYLOOK_MARBLE_THREADS` 控制，详见 `rust/README.md`。

### 重新标定（换软件版本时）

1. 合成色卡已经放在 `chart/` 里，可以直接用。也可以用 `python3 capture_one/make_chart.py SOURCE.ARW` 从自己的 A7M5 照片重新生成。
   - 内容是 48 级灰阶加 6³ 立方体，原 ARW 只读不改。
   - 每个色块按 Sony 前端增益反算写入，所以线性值精确已知。
   - 文件里三张内嵌 JPEG（缩略图、预览、全尺寸）也换成了色卡的 ST 外观渲染。
2. **Capture One**：
   - 运行 `c1_look_profile.py --probes`，安装生成的探针配置；
   - 用 Probe 导出色卡，运行 `measure_chart.py` 测量，再运行 `fit_c1_encoding.py` 拟合；
   - 用 Probe-Sq / Probe-Mono 检查 ICC 之后还有没有处理。
3. **Camera Raw**：
   - 运行 `acr_profiles.py probes`，安装生成的探针配置；
   - 用 Probe 导出色卡（16 位 ProPhoto，关闭镜头校正），运行 `measure_acr_chart.py` 测量，再运行 `acr_encoding.py` 拟合。
4. **DxO PhotoLab**：
   - 运行 `dxo/dxo_profiles.py probes` 生成探针 LUT，`dxo/dxo_profiles.py base2` 生成底座 DCP；
   - 用 Probe 导出色卡（16 位 TIFF、ProPhoto RGB、不勾 Preserve color details），运行 `measure_acr_chart.py` 测量；
   - 用测量结果检验 `dxo/dxo_model.py` 里的模型。
5. 照片对比：`compare_c1_export.py` / `compare_acr_export.py EXPORT.tif EDIT.tif`；拼对照图用 `compare_figure.py`。

## 目录

| 目录             | 内容                                                                          |
| -------------- | --------------------------------------------------------------------------- |
| `profiles/`    | 生成好的配置：C1 的 ICC，ACR 的 DCP 和 XMP，DxO 的 DCP 和 `.cube`                          |
| `chart/`       | 合成色卡：`SonyChart.ARW`（可直接用 C1 / ACR 打开）、色块定义 `SonyChart.json`、预览图            |
| `sonylook/`    | Sony 外观链与前端的 Python 移植，以及 ARW / Edit 读取器。另含 RawNR、DRO、锐化 / Spica、Marble、镜头几何校正（都不用于配置）、BW/SE 的 Effect 阶段（用于配置），以及整图解码器 `sony_decode.py` |
| `rust/`        | 可选的 Rust 内核（`sonylook_core`）：融合渲染 band、GeoCorr、ITP、Marble、SSCS 和表构造。见 `rust/README.md` |
| `capture_one/` | C1 配置生成、合成色卡、测量与对比                                                          |
| `camera_raw/`  | ACR 表格式、DCP 读写、配置生成、测量与对比                                                   |
| `dxo/`         | DxO PhotoLab 的 LUT 输入模型（`dxo_model.py`）、探针和底座 DCP 生成、LUT 生成                      |
| `docs/`        | 研究记录：处理链细节、C1、ACR 和 DxO 的标定过程。文中提到的证据文件、测试导出不在本仓库                               |

## 说明

- 本项目与 Sony、Capture One、Adobe、DxO 均无关联，相关名称和商标归各自所有者所有。
- 本项目通过分析软件行为来实现互操作。仓库不包含这些软件的二进制、反编译代码或从中提取的数据；需要的常量都在使用者本机从其自己安装的软件中读取。
- 配置文件中的 ColorMatrix 数值取自 Adobe Standard 的 A7M5 配置，用于让 ACR 的白平衡行为保持不变。DxO 的底座 DCP 出于同样的原因，用了 Adobe Standard 的 ColorMatrix 和 D65 ForwardMatrix。
- `dxo/dxo_model.py` 按观测到的行为重新实现了 PhotoLab 的高光恢复，并包含几个在 PhotoLab 运行时测得的数值（DxO Wide Gamut 矩阵、高光白点）；不包含 DxO 的代码或文件。

## 许可

- 代码、文档和生成配置的工作以 [MIT 许可证](LICENSE) 发布。
- 配置文件按原样提供。其中源自第三方的数据（Sony 的外观参数、Adobe 的 ColorMatrix 数值）不在本许可的授权范围内，本项目也不对其主张任何权利。

## 合成色卡

![合成色卡（Sony ST 外观渲染）](chart/SonyChart_preview.png)

`chart/SonyChart.ARW` 是一张合法的 A7M5 ARW：

- raw 数据换成了 264 个已知线性值的色块：48 级灰阶，加上相机 RGB 的 6×6×6 立方体；
- 元数据（白平衡、黑电平、镜头等）保持原照片的值；
- 每个色块的 raw 值、Sony 线性值和位置见 `chart/SonyChart.json`。
