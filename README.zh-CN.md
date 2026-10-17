# Sony A7M5 创意外观 → Capture One / Camera Raw

[English](README.md) | 中文

天下苦创意风格久矣，CameraRaw 内迟迟没有上线A7M5的创意风格稳健

本仓库通过逆向Sony Image Edit，把 Sony α7 V（ILCE-7M5）的 12 种 Creative Look移植成 Capture One 和 Camera Raw / Lightroom 的配置，并通过对比检验，色度上基本没有差异。

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
- 覆盖范围：只有 ST、FL2、FL3 有 Edit 导出图可以对照。其余 9 种外观走同一套已验证的代码，只是数据不同，但没有和 Sony 的成片核对过。上表 FL2/FL3 的数字是在褪色为 0 时测的，Edit 渲染这两种外观时用的就是 0。从 v1.0.2 起，配置文件按相机默认的褪色生成（FL2/FL3 为 1，IN 为 3，SH 为 6；Edit 漏了 FL2/FL3 的这个默认值）。这一改动还没有和相机直出 JPEG 对照过。

## 安装使用

### Capture One

1. 把 `profiles/capture_one/SonyA7M5-Look-*.icc` 复制到 `~/Library/ColorSync/Profiles/`，然后**重启** Capture One。C1 会缓存已加载的配置。
2. 在 Base Characteristics 中，ICC Profile 选 `SonyA7M5 Look XX`，**Curve 选 Linear Response**。

### Camera Raw / Lightroom

1. 把 `profiles/camera_raw/SonyA7M5 Linear Base.dcp` 复制到 `~/Library/Application Support/Adobe/CameraRaw/CameraProfiles/`。
2. 把 `profiles/camera_raw/SonyA7M5 Look *.xmp` 复制到 `~/Library/Application Support/Adobe/CameraRaw/Settings/`。
3. 重启 Photoshop / Lightroom，在配置文件浏览器的 **Sony A7M5** 组里选择外观。

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
- SSCS（把又亮又饱和的颜色向亮度收拢）已移植（`sonylook/sony_sscs.py`），但配置文件还没有用上。Edit 的 3D LUT 和色相/饱和度两个阶段在默认参数下不运行。

### 2. 查找表表示

由于 *f* 是逐像素映射，可以在输入空间的规则网格上预先求值，用插值近似任意输入。

- Capture One 版本：输入 ICC 配置的 A2B0 表，65³ 网格，配合平方根形状曲线，以提高暗部的采样密度。
- Camera Raw 版本：XMP 配置中的 RGBTable，64³ 网格。

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

两点补充：

- Capture One 的 *E* 依赖所选影调曲线，配置只在 Linear Response 下有效。
- Camera Raw 的底座 DCP 将 ForwardMatrix 设为线性 ProPhoto → XYZ D50，影调曲线设为恒等，使 Camera Raw 的工作空间与白平衡后的相机 RGB 一致，避免叠加 Adobe 自身的色彩和影调。

各部分的推导过程和中间结果见 [docs/pipeline.md](docs/pipeline.md)、[docs/capture_one.md](docs/capture_one.md)、[docs/camera_raw.md](docs/camera_raw.md)。

## 重新生成

需要：Python 3.11+、`pip install -r requirements.txt`、任意一张 A7M5 的 ARW，以及本机安装的 **Imaging Edge Edit 4.0.00.10312**（macOS）。

外观链用到的一些常量表（色相 atan 表、对比度曲线族等）是在运行时从你本机安装的 Edit 里读取的（`sonylook/edit_binary.py`），本仓库不包含任何 Sony 的二进制或数据。脚本会校验 arm64 部分的 SHA-256；其他版本的表地址不同，无法使用。路径可以用 `SONY_EDIT_BINARY` 指定。

```bash
python3 capture_one/c1_look_profile.py /path/to/A7M5.ARW
```

```bash
python3 camera_raw/acr_look.py /path/to/A7M5.ARW --write
```

```bash
python3 sonylook/sony_render.py /path/to/A7M5.ARW Standard FL2 --scale 4
```

- 第一条生成 C1 配置，第二条生成 ACR 配置，第三条用外观链直接渲染预览。
- 用 DSC03591 重新生成的结果，与 `profiles/` 里的文件逐字节相同。

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
4. 照片对比：`compare_c1_export.py` / `compare_acr_export.py EXPORT.tif EDIT.tif`；拼对照图用 `compare_figure.py`。

## 目录

| 目录             | 内容                                                                          |
| -------------- | --------------------------------------------------------------------------- |
| `profiles/`    | 生成好的配置：C1 的 ICC，ACR 的 DCP 和 XMP                                             |
| `chart/`       | 合成色卡：`SonyChart.ARW`（可直接用 C1 / ACR 打开）、色块定义 `SonyChart.json`、预览图            |
| `sonylook/`    | Sony 外观链与前端的 Python 移植，以及 ARW / Edit 读取器。另含 RawNR、DRO、锐化 / Spica、Marble、镜头几何校正（都不用于配置）以及 BW/SE 的 Effect 阶段（用于配置） |
| `capture_one/` | C1 配置生成、合成色卡、测量与对比                                                          |
| `camera_raw/`  | ACR 表格式、DCP 读写、配置生成、测量与对比                                                   |
| `docs/`        | 研究记录：处理链细节、C1 和 ACR 的标定过程。文中提到的证据文件、测试导出不在本仓库                               |

## 说明

- 本项目与 Sony、Capture One、Adobe 均无关联，相关名称和商标归各自所有者所有。
- 本项目通过分析软件行为来实现互操作。仓库不包含这些软件的二进制、反编译代码或从中提取的数据；需要的常量都在使用者本机从其自己安装的软件中读取。
- 配置文件中的 ColorMatrix 数值取自 Adobe Standard 的 A7M5 配置，用于让 ACR 的白平衡行为保持不变。

## 许可

- 代码、文档和生成配置的工作以 [MIT 许可证](LICENSE) 发布。
- 配置文件按原样提供。其中源自第三方的数据（Sony 的外观参数、Adobe 的 ColorMatrix 数值）不在本许可的授权范围内，本项目也不对其主张任何权利。

## 合成色卡

![合成色卡（Sony ST 外观渲染）](chart/SonyChart_preview.png)

`chart/SonyChart.ARW` 是一张合法的 A7M5 ARW：

- raw 数据换成了 264 个已知线性值的色块：48 级灰阶，加上相机 RGB 的 6×6×6 立方体；
- 元数据（白平衡、黑电平、镜头等）保持原照片的值；
- 每个色块的 raw 值、Sony 线性值和位置见 `chart/SonyChart.json`。
