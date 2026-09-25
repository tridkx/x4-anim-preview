# x4-anim-preview

**在游戏外播放《X4：基石》的真实 NPC 动画，用来检查人物替换 mod 的动作是否正常。**

做 X4 人物替换 mod 时，每改一版都得进游戏、跑到 NPC 面前、盯着看几秒——慢且看不出细节。
这个工具直接读游戏里的 `.xsm` 动画和 `.xac` 网格，在本地把你的 mod 资产和 vanilla
**并排播放同一条动画**，关节撕裂、四肢弯错方向、猫步、手/脖子错位这些一眼就能看到。

![界面](docs/screenshot.png)

左边是你的 mod，右边是 vanilla，同一条动画、同一时刻。

## 运行方式

| 方式 | 命令 | 适合 |
|---|---|---|
| **图形界面**（推荐） | 双击 `run_studio.bat`，或 `python tools/studio.py` | 日常检查：窗口里直接选 mod、搜动画、拖时间轴 |
| 命令行 | `python tools/viewer.py --mod <目录>` | 脚本化、批处理、快速看一眼 |

---

## 1. 为什么能这么做

X4 的 NPC 是**换网格、留骨架**：`libraries/character_components.xml` 里的共享
component（比如 `character_argon_female_01`）持有整套 Biped 骨架和 180 多条动画，
macro 只挑 head / torso / props 三个**网格**槽位。所以：

- 你的 mod 网格用的就是游戏里那套骨架，**游戏的动画可以直接拿来驱动它**；
- 检查"动作对不对"不需要启动游戏，只要把同一条 `.xsm` 套上去播放。

本仓库自带 `.xsm` 格式的解析实现（逆向所得，见下），不依赖 3ds Max 或官方插件。

## 2. 快速开始

```bash
pip install -r requirements.txt        # numpy / pillow / pyglet
cp config.example.json config.json     # 按需改 game_dir（也可用环境变量 X4_GAME_DIR）
```

### 图形界面（推荐）

双击 **`run_studio.bat`**，或者：

```bash
python tools/studio.py
```

![控制面板](docs/screenshot-panel.png)

左边是控制面板，右边是独立的三维预览窗口：

1. **选择 mod** —— 自动列出机器上的 mod（游戏 `extensions/` 里的、以及工作区里各工程的
   输出目录），也支持「浏览…」手动指定；勾上「与 vanilla 并排对比」就左右分屏。
2. **播放控制** —— 播放/暂停、逐帧、时间轴拖动、速度、循环、**轮播**（自动过一遍所有动画）、
   网格/骨骼显示、重置相机、一键截图。
3. **选择动画** —— 该角色 component 的全部动画（默认 186 条），带搜索框过滤。

选择会被记住（`config.json`），下次打开就是你上次看的 mod 和动画。

### 命令行

```bash
python tools/viewer.py --vanilla                 # 只跑 vanilla，确认工具链正常
python tools/viewer.py --mod /path/to/your/mod   # 与 vanilla 并排对比
python tools/viewer.py --body a.xac --head b.xac # 指定具体资产

# 不开窗口，批量出图
python tools/viewer.py --mod /path/to/mod --anim anim_stand_idle_05 \
    --shot out.png --frames 0,15,30,45
```

**窗口操作**

| 键 | 作用 |
|---|---|
| 空格 | 播放 / 暂停 |
| ← → | 上一条 / 下一条动画 |
| `,` `.` | 逐帧后退 / 前进（按 15 fps） |
| ↑ ↓ | 播放速度 |
| `B` | 叠加骨骼线框（穿透显示，便于核对骨架） |
| `N` | 只看骨架（隐藏网格） |
| `R` | 重置相机 |
| 鼠标拖动 / 滚轮 | 旋转 / 缩放 |
| `Q` / `ESC` | 退出 |

## 3. 骨架校验

动画能播的前提是 mod 资产带着**和 vanilla 一模一样**的骨架。单独查这个：

```bash
python tools/skeleton_check.py --mod /path/to/your/mod
```

输出每根骨的 bind 位置 / 旋转 / 缩放偏差与父节点差异，并给出结论。判据是
**按骨名配对**而不是按出现顺序——原版资产里同一套骨经常重复多次，按顺序比会误报。

## 4. 资产与动画从哪来

- 网格：`--mod` 目录下的 `.xac`（按文件名含 `head` / `body` 分槽位），也可以给游戏包内路径。
- 动画：默认取 macro 实际引用的 component（`character_argon_female_01`）。
  Argon / Terran 等人类种族共用这套骨架和动画，所以 Terran 的 mod 也用它。
  `python tools/animations.py <component名>` 可以列出任意 component 的动画清单。

## 5. `.xsm` 格式（逆向结果）

```
"XSM " + u32 版本 + 头部字段 + 导出器/源文件/日期三个字符串
随后 N 条节点记录首尾相接：
    [100 字节记录头][u32 名字长度][名字][位置关键帧段][旋转关键帧段]
```

记录头里与帧数有关的部分固定在 `名字长度字段 - 0x50`：

| 偏移 | 含义 |
|---|---|
| +0x00 | u32 位置关键帧数 |
| +0x04 | u32 旋转关键帧数 |
| +0x08 / +0x0c | 附加段帧数（老格式才有意义） |
| −0x04 | 单精度浮点（骨骼半径一类，未使用） |

关键帧有两种编码，同一文件内一致：

| 布局 | 记录头 | 位置段 | 旋转段 | 附加 |
|---|---|---|---|---|
| **新**（X4 9.00 常见） | 100 B | 16 B/帧 `f32 x,y,z,t` | 12 B/帧 `i16 x,y,z,w`(Q15) + `f32 t` | — |
| **老**（2008–2011 导出） | 100 B | 同上 | 20 B/帧 `f32 x,y,z,w,t` | 20 B/帧的同构轨道 + 32 B 尾 |

- 坐标系与 `.xac` 一致：**Y 轴向上、单位厘米**，旋转是**相对父骨的局部旋转**。
- 旋转段存的是**绝对局部旋转**（第 0 帧实测与 `.xac` 的绑定旋转一致），直接套用即可。
- 关键帧时间是稀疏的（不保证等间隔），按时间插值；四元数用相邻帧 nlerp。
- **现代资产的节点名与 `.xac` 骨架同名**（`Bip01 ...`）；老资产用
  `root/spine1/l_upleg` 旧命名，和现役骨架对不上，只能作参考。

实现里对上述差异做了自适应：先在文件头部定位第一条记录并判定布局，之后按记录长度
**链式推进**（不扫全文件），落后时在 ±24 字节内重新对齐。

> 顺带记录一个 `.xac` 的坑：**材质 chunk 的 length 不包含 layer 数据**
> （实测 `length=129` 的 chunk 实际占 191 字节），照着 length 跳会立刻错位。

## 6. 参数速查

```
--mod DIR              mod 目录（自动挑 head/torso 的 .xac）
--body / --head FILE   直接指定资产（磁盘路径或游戏包内路径）
--vanilla              只加载 vanilla 基准
--vanilla-body/--head  换对照用的 vanilla 槽位资产
--component NAME       取哪套动画（默认 character_argon_female_01）
--anim NAME            只播某条动画（逻辑名或资产名）
--delta                实验性增量姿态模式，见下
--shot OUT.png         不开窗口，直接出图；配合 --frames/-–anims/--azimuth
--width/--height       窗口或出图尺寸
```

**关于 `--delta`**：本意是用"相对 `.xsm` 记录头里绑定姿态的增量"去抹平动画文件与
网格资产的版本差。实测**不该当默认**：同一动画在不同资产上会给出不同姿态。
默认是直接套用动画值。

## 7. 已知限制

- 只做**姿态**：不做贴图、材质通道、透明/双面，也不做法线贴图；检查外观请回游戏。
- 只播**骨骼动画**：morph / 表情（viseme、blendshape）还没做，脸部细节看不到。
- props 槽位（头发、胡子等）可以当普通资产用 `--body/--head` 传，但没有自动配对。
- 老格式动画（`root/spine1` 命名）不能驱动现役骨架，播放时会提示匹配率。
- 软光栅出图是 CPU 实现，一张 400×660 约 0.25 s；实时窗口走 OpenGL，不吃这个开销。

## 8. 目录

```
run_studio.bat      双击启动图形界面
tools/
  studio.py         图形界面（tkinter 控制面板 + 独立 3D 窗口）   <- 推荐入口
  viewer.py         命令行预览 / 批量出图
  scene.py          资产定位、场景组装、自动发现 mod 目录
  glview.py         OpenGL 视图层：轨道相机、绘制、可手动步进的窗口
  x4game.py         游戏目录探测 + .cat/.dat 随机读取
  xac.py            .xac 解析（骨架 / 网格 / UV / 骨骼权重）
  xsm.py            .xsm 解析（关键帧曲线）
  rig.py            正向运动学 + 线性混合蒙皮
  animations.py     component -> 动画清单
  skeleton_check.py 骨架一致性校验
  render.py         纯 numpy 软光栅（离线出图用）
examples/
  offline_check.py  不开窗口跑一遍并出图的最小示例
```

图形界面用的是 tkinter（Python 自带）做控制面板、pyglet 单独开三维窗口，
两者共享一个线程：tkinter 的 `after` 循环每帧调用 `PreviewWindow.pump()`
手动步进 pyglet，因此不需要两套事件循环，也不用加锁。
