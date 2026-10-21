# 蛋仔派对 PC（NeoX 引擎）NPK 解包工具

针对 2026 年版本（`patch_pc_202610141744_4369854`，package_version 1.0.32）的 NPK 格式，
可在 Windows 上把 `gui2.npk` 等资源包解出为原始资源文件（KTX 纹理、XML/JSON 配置、模型/动画、Wwise 音频等）。

---

## 一、2026 版 NPK 的结构（与旧版工具的区别）

```
偏移   类型        内容
0x00   char[4]     "NXPK"
0x04   u32         entry_count            条目数
0x08   u32         toc_offset_hi          TOC（文件表）在文件内的偏移高 32 位
0x0C   u32         entry_encryption_mode  = 3
0x10   u32         hash_mode              = 0
0x14   u32         toc_offset_lo          TOC 偏移低 32 位
0x18   ...         数据区
末尾   entries*28  TOC：每条 28 字节
```

TOC 位置 = `(toc_offset_hi << 32) | toc_offset_lo` = `文件大小 - entry_count*28`，
即文件表被加密后放在文件最末尾（本工具读到的 TOC 里全是高熵随机字节，AES/RC4/XXTEA/异或等
上百种组合都试过，密钥是引擎运行时从内存派生的，`client.exe` 里没有硬编码 —— 所以**纯静态解包不可行**）。

每条 TOC 记录（28 字节，小端）：

| 字段 | 含义 |
|---|---|
| u32 id | 路径的 murmur3_32 哈希（路径用反斜杠、seed `0x9747B28C`） |
| u32 offset | 数据在 npk 内的偏移 |
| u32 packed_size | 压缩后大小 |
| u32 raw_size | 原始大小 |
| u32 checksum_packed | 校验 |
| u32 checksum_raw | 校验 |
| u32 flags | 低字节=压缩方式：0 存储、1 zlib、2 lz4、3 zstd；bit24 置位=数据还做了额外加密 |

所以正确做法是：**先从正在运行的游戏进程内存里抓取已解密的 TOC**（`harvest` 命令），
之后 `info` / `list` / `extract` 全部可以离线完成，不再需要开游戏。

---

## 二、目录说明

| 文件 | 作用 |
|---|---|
| `npk_unpack.py` | 主工具：`harvest` / `info` / `list` / `extract` |
| `npk_zstd.js` | 用 node.js 批量解 zstd（Python 标准库不带 zstd） |
| `ktx2png.py` | KTX（DXT1/DXT3/DXT5）→ PNG 转换，供 `--convert-ktx` 调用 |
| `mk_names.py` | 用一份路径清单还原资源名（murmur3 反查），生成 `names.txt` |
| `toc/` | 本版本（1.0.32）已经抓好的 10 个包的解密 TOC |
| `names.txt` | 从游戏日志/已解出文本中还原的 262 条 资源 id→路径 映射 |
| `demo_gui2_ui/` | 示例：gui2 前 60 条 + 若干条纹理的解包结果（含 ~42 张 PNG 和拼接图 `_montage.png`） |
| `demo_char_named/` | 示例：带资源名的 char 包解包结果 |

> 需要 Python 3.8+；zstd 解压需要 Node.js（或指定 `--node`）。本机 DeepSeek 运行环境自带的 node 也可用：
> `C:\Users\cfgxd\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\node\bin\node.exe`

`toc/` 里已包含本版本可直接使用的 10 个包：

| 包 | entries | 说明 |
|---|---|---|
| gui2 | 128551 | UI 资源（本次目标包） |
| char | 148779 | 角色模型/动画 |
| props | 83980 | 道具 |
| res | 118440 | 通用资源 |
| scene | 29053 | 场景 |
| char_loading | 22291 | 加载界面角色 |
| script | 48751 | 脚本（加密 pyc） |
| wwise | 5793 | 音频（Wwise） |
| props_zhucheng | 338 | 主城道具 |
| props_ui_model | 337 | UI 模型 |

> `script_patch.npk` 不会被运行时加载，游戏内存里没有它的 TOC，本版本未附带。
> TOC 与游戏版本一一对应；`harvest` 已实测可重抓出与本目录逐字节相同的 TOC（SHA256 一致）。

---

## 三、快速开始

### 1) 抓取 TOC（只需在游戏更新后做一次）

先启动游戏，保持 `client.exe` 在运行（启动器打开或直接运行
`D:\FeverApps\party_pc\bin\client.exe`，工作目录设为 `D:\FeverApps\party_pc`），
等进入大厅后执行：

```bat
python npk_unpack.py harvest --game-dir D:\FeverApps\party_pc
```

约 10~20 秒内会打印并保存：

```
  found gui2       entries=128551 va=0x... -> toc\toc_gui2.bin
  ...
saved 10 TOC(s) into ...\toc
```

### 2) 查看包信息

```bat
python npk_unpack.py info D:\FeverApps\party_pc\gui2.npk
```

```
gui2.npk: size=5994582588 entries=128551 toc_offset=0x1651725f8 entry_encryption_mode=3 hash_mode=0
entries: 128551  stored=3 zstd=107849 zlib=0 lz4=0 other=0 encrypted(bit24)=20699 invalid=1
ids: min=00000b4c max=ffffcc53 unique=128551 sorted=True
packed sum=5990789222 (5713.3 MiB)  raw sum=75726504073 (72218.4 MiB)
```

### 3) 列出条目

```bat
python npk_unpack.py list D:\FeverApps\party_pc\gui2.npk --limit 20
```

### 4) 解包

```bat
:: 解前 200 条并顺便把 KTX 转成 PNG
python npk_unpack.py extract D:\FeverApps\party_pc\gui2.npk --out out\gui2 --limit 200 --convert-ktx

:: 只解指定的几条（TOC 里的下标）
python npk_unpack.py extract D:\FeverApps\party_pc\gui2.npk --out out\gui2 --indices 0-50,100,300

:: 带资源名解包（需要 names.txt；用 mk_names.py 生成）
python npk_unpack.py extract D:\FeverApps\party_pc\char.npk --out out\char --indices 154,2110 --names names.txt

:: 全解（慎用！gui2 raw 总和约 70 GiB）
python npk_unpack.py extract D:\FeverApps\party_pc\gui2.npk --out out\gui2 --all --yes
```

#### 4.1) 大包推荐用批量流式解包（快很多）

`npk_unpack.py extract --all` 会把所有压缩数据先写一遍临时文件，机械盘上很慢。
`extract_all2.py` 按 **文件 offset 顺序**读包、按块把压缩数据拼成一个 blob 写到系统盘
临时目录，再让 node 一次解完整块并**直接**写到目标盘（输出按 id 低字节分 256 个子目录），
避免反复随机读写和二次拷贝：

```bat
:: 全解 gui2 到 D:\gui2（约 68 GiB、10 万+ 文件，机械盘约 20~40 分钟）
python extract_all2.py D:\FeverApps\party_pc\gui2.npk --out D:\gui2

:: 断点续跑（已解出的条目按 index.csv 跳过）
python extract_all2.py D:\FeverApps\party_pc\gui2.npk --out D:\gui2 --resume

:: 解出后批量把 KTX 转 PNG（多进程、可断点续跑、可选删源）
python ktx_all.py --dir D:\gui2 --workers 6
python ktx_all.py --dir D:\gui2 --workers 6 --delete
```

输出结构：`<out>\<id低字节hex>\<下标6位>_<id8位>.<ext>`，根目录还有
`index.csv`（下标,id,packed,raw,类型,相对路径）和 `summary.json`（条目数/字节数/耗时）。

常用参数：

| 参数 | 说明 |
|---|---|
| `--limit N` / `--indices A-B,C` / `--all` | 选择条目 |
| `--max-raw N` | 跳过 raw_size 大于 N 的条目（限制体量） |
| `--types ktx,xml,json` | 只解某类资源（需先读条目头判断类型，大包+机械盘较慢，建议配合 `--indices/--limit`） |
| `--convert-ktx` | 顺带把 KTX 转成 PNG（DXT1/3/5） |
| `--names names.txt` | 按还原出的路径输出目录结构 |
| `--include-encrypted` | 把 bit24 加密条目原样导出为 `.enc` |
| `--node <path>` | 指定 node.exe 路径 |

---

## 四、资源命名（可选）

NPK 里不存文件名，只存 `id = murmur3_32(相对路径, seed 0x9747B28C)`。
只要拿到任何路径清单（游戏日志、已解出的 XML/JSON 里引用的其他资源、社区清单……），
就能反查名字：

```bat
python mk_names.py --out names.txt --toc-dir toc ^
  --logs D:\FeverApps\party_pc\filetrace.log ^
         D:\FeverApps\party_pc\log.txt ^
  --text-dir out\gui2_text
```

本仓库的 `names.txt` 就是从 `filetrace.log` + 已解出的文本资源里还原出来的
（353 个候选路径命中 262 条，命中率 74%，覆盖 char/props/res/scene/props_zhucheng）。
gui2 此前没有现成路径清单，所以 gui2 的资源名大多还是 `下标_id.扩展名` 形式；
你可以把解出来的 XML/JSON 当输入再跑一次 `mk_names.py --text-dir out\gui2` 继续扩充。

---

## 五、已知限制

1. **gui2 有 20699 条额外加密条目**（`flags = 0x01000000`，占 16%）：数据先被加密再（可能）压缩，
   目前未破解，工具会跳过（`--include-encrypted` 可原样导出 `.enc` 待研究）。
2. **script.npk 是加密的 NeoX pyc**（`neox-py-encrypt-v2`），条目本身存储未压缩，但导出后是
   加密的 pyc/字节码容器（头部 `58 4E 18 02`），需要额外的 pyc 解密器才能反编译。
3. **gui2 全解体积巨大**：raw 总和约 72 GiB（UI 纹理常为 2048/4096 乃至 5700 分辨率 DXT5），
   建议用 `--indices` / `--max-raw` / `--types` 按需解。
4. **TOC 与版本绑定**：`toc/*.bin` 对应 1.0.32（2026-10-14 patch）。游戏更新后条目数变化，
   重新跑一次 `harvest` 即可（几秒钟）。
5. 没有做 KTX 以外的格式转换（模型/动画是 NeoX 私有格式）。

---

## 六、原理验证记录（可选阅读）

- 11 个 npk 的头全部满足 `文件大小 - ((hi<<32)|lo) == entries*28`，TOC 位于文件末尾。
- TOC 密文熵 7.99，无周期；与明文 TOC 异或得到的密钥流熵 7.9999、无重复；
  `client.exe` 中搜不到该 16 字节常量，说明密钥运行时派生 → 静态解密不可行，改走内存抓取。
- 从游戏进程内存中抓到 10 个 NPK 对象（`NXPK` 头 + 3 个 u64 指针 + 16 字节常量），
  其中 u0/u1 指向**已解密的 TOC 缓冲区**，直接 dump 出来即为明文 TOC（已用条目合法性验证）。
- 数据条目：flags=3 为 zstd 帧（28 B5 2F FD），用 Node 的 `zlib.zstdDecompressSync` 解压；
  flags=0 为存储（如 wwise 音频、script pyc）。
- 纹理：KTX 1.1 容器，glInternalFormat = 0x83F3（DXT5/BC3），
  转成 DDS 头后交给 Pillow 即可保存为 PNG（`ktx2png.py`）。
- 名称：对 `filetrace.log` 里的路径做 `murmur3_32(seed=0x9747B28C)` 后与 TOC id 精确匹配，
  命中率 100%（对已覆盖的包），证实 id 算法。
