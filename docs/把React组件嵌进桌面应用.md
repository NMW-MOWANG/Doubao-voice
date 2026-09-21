# 把 React 组件嵌进桌面应用

—— 豆包语音输入浮标（GTK3 / cairo → WebKit2 + React）这次改造的经验总结。

面向的场景：桌面应用里有一块**视觉要求很高**的东西（动效、模糊、渐层、光晕、WebGL），
原生绘制写起来痛苦或根本写不出来，而团队在 Web 那边已经有现成组件（这次是
`voice-glow` 的光束 + `metal-fx` 的金属环）。要问的是：**怎么把它嵌进去，而不是重写一遍。**

结论先放这里：**这条路能走通，前提是「宿主接口」设计成和原来一模一样，并且渲染器可以随时退回原来的实现。**
代价是内存和 CPU（这次实测：常驻 ≈ 550MB，显示时 ≈ 1 个核），必须显式接受。

---

## 1. 方案选型：为什么是 WebKit2GTK 嵌本地页面

| 方案 | 结论 |
| --- | --- |
| 用 cairo/原生重写特效 | 光束的三层结构（描边 + 内光 + 模糊晕）靠 CSS `filter: blur()` 和渐层实现，cairo 里做模糊要自己上卷积或依赖 `xrender`，成本远高于收益 |
| 自带 Chromium（Electron/Tauri） | 为一个 340×60 的浮标拖进一整个运行时，体积和内存都不划算 |
| 让特效跑在系统浏览器里 | 做不到"永远置顶 + 不抢焦点 + 点击穿透" |
| **WebKit2GTK 嵌本地 HTML** | **选了这条**：GTK 生态自带（`gir1.2-webkit2-4.1`），能拿到和普通 GTK 窗口同样的窗口属性，进程开销可控 |

关键前提：这块 Web 内容**必须能在一个普通 X11/GTK 窗口里活下来**，宿主对窗口的要求
（override-redirect、不抢焦点、点击穿透、透明）在 WebKit 里全都成立——这次专门写探针
（`prototype/webkit_probe.py`）先把这一点验了：WebGL2 可用、窗口 `depth=32`、
活动窗口全程没被换掉、进程树 RSS/CPU 能量出来。

---

## 2. 架构：宿主接口保持不变，只换「怎么画」

```
ui.py / engine.py                     ← 原来的主进程（Wayland）
   │  overlay.show(state, text) / set_level(level)
   ▼
overlay.py                            ← 主进程侧，拉起子进程
   │  stdin: JSON 行  {"cmd":"show","state":"recording","text":"正在录音"}
   ▼
overlayd.py  ── 起一个 GTK 窗口（override-redirect / 不抢焦点 / 点击穿透 / 底部居中）
   │
   ├── cairo 渲染器：自己画胶囊 + 圆环 + 麦克风 + 文字（原路，保留为回退）
   └── WebKit 渲染器：WebKit2.WebView 加载 dist/embed.html
          │  evaluate_javascript("window.voiceOverlay.apply({...})")
          ▼
        页面（React）：window.voiceOverlay.apply(frame) → setState / 写 ref
```

三条设计决定，后面每一条都省了事：

1. **协议不变**。页面吃的帧和 overlayd 从 stdin 收到的是**同一套 JSON**（`show` / `level` /
   `text` / `hide`）。宿主侧一行没改，两种渲染器共用同一套驱动逻辑，回退是"换个类"而不是"改一整套"。
2. **页面自己暴露命令式接口**，而不是让宿主去操作 DOM：
   ```ts
   window.voiceOverlay = { apply(frame), debug() };
   ```
   `debug()` 是特意留的：它让宿主/测试能问"页面现在什么状态"，是把"嵌进去的页面"
   从黑盒变白盒的关键一步（本次大半问题都是靠它定位的）。
3. **渲染器可切换 + 自动回退**。默认 `auto`：构建产物在且 WebKit 可用就用 WebKit，
   否则退回 cairo；页面加载失败、WebKit 起不来也退回 cairo，而不是留一个空白浮标。
   桌面工具宁可贵一点，不能"看起来在跑其实没画"。

### 状态怎么进 React

- 低频、要触发重渲染的（阶段、文字）→ `setState`。
- 每帧都要读、但不该重渲染的（音量 0–1）→ **`useRef`**，用 `level={() => ref.current}` 这种
  取数器交给特效库自己每帧采样。这是 `voice-glow` 的既有约定，嵌进来时必须沿用，
  否则 12–60 Hz 的 setState 会自己把 CPU 吃光。
- 页面就绪的握手：页面挂载后改 `document.title = "voice-overlay-ready"`，宿主监听
  `notify::title`，收到才把攒着的帧灌进去。比轮询简单，也不用额外开 JS 通道。

---

## 3. 踩过的坑（这部分比结论有用）

每个都按「现象 → 怎么定位 → 根因 → 修法」记。

### 坑 1：`file://` + `crossorigin`，页面资源全部加载失败

- **现象**：页面框架在，但 React 完全没渲染，`document.body.innerHTML` 只有一个空的 `#root`；
  `performance.getEntriesByType('resource')` 是空的。
- **定位**：`window.addEventListener('error', handler, true)`（**捕获阶段**！）才抓得到资源加载失败：
  `resource fail | SCRIPT | .../assets/index-xxx.js`，`LINK` 同理。
- **根因**：Vite 构建产物默认给 `<script type="module">` 和 `<link rel="modulepreload">` 带上
  `crossorigin` 属性。`file://` 页面的源是 opaque 的，带 CORS 属性的子资源加载被拒绝。
- **修法**：宿主给 WebView 开 `settings.set_allow_file_access_from_file_urls(True)`。
  （等价替代：把构建产物用本地 HTTP 服务端出去，或在打包时去掉 `crossorigin`。）
- **教训**：**先确认"代码到底跑了没有"，再去看渲染**。资源没加载和组件没画出来，症状都是"白屏"。

### 坑 2：窗口隐藏时，页面视口是 0×0

- **现象**：浮标窗口出现时是空的，React 甚至没渲染出节点。
- **定位**：在真实窗口里问页面尺寸：`[window.innerWidth, window.innerHeight]` → 隐藏时 `[0,0]`，
  `show_all()` 之后才 `[340,60]`。
- **根因**：浮标进程按"等第一条指令再建窗口"的逻辑走，页面加载时窗口还没映射，视口是 0×0；
  于是按容器尺寸建的 canvas 是 **0×0**，而 WebKit 下 0 尺寸 canvas **拿不到 WebGL context**，
  特效组件直接抛错（见坑 3）。
- **修法**：页面里这块东西的尺寸**写死**（`340×60`），不要用 `100%` / 依赖视口。
  在"窗口还没映射就加载页面"这个时间窗里，写死的尺寸是唯一可靠的。

### 坑 3：组件抛错会把整棵 React 树卸掉

- **现象**：一个特效组件抛错，整页空白——**比"没有特效"更糟**，因为连基本的胶囊都不见了。
- **根因**：React 没有错误边界时，渲染期抛错会卸载整棵树。
- **修法**：两层错误边界：
  - 外层裹住整个特效树，fallback 是"普通胶囊"（宿主语言重绘一版的等价物）；
  - 内层只裹住高风险的那个子组件（这次的金属环），它挂了不影响父层的光束。
  再配一个**能力探测**，能提前判断就别让它抛（见坑 4）。

### 坑 4：特效库的「支持性探测」和「真实渲染路径」不一致

- **现象**：`isMetalFxSupported()` 返回 `true`，但组件照样抛 `WebGL2 not supported`。
- **定位**：读库的源码（minified 也读得出来）——它有两套路径：
  ```js
  const n = typeof OffscreenCanvas < "u";
  if (n) gl = new OffscreenCanvas(w, h).getContext("webgl2", {alpha, premultipliedAlpha, antialias:false});
  else   gl = canvas.getContext("webgl2", {alpha, premultipliedAlpha, antialias:false, preserveDrawingBuffer:true});
  if (!gl) throw new Error("metal-fx: WebGL2 not supported");
  ```
  而它的 `isSupported()` 用的是**普通 canvas** 探的。WebKitGTK 偏偏"有 `OffscreenCanvas`，
  但不支持在上面创建 WebGL2"。
- **修法**：嵌入页在加载前把 `OffscreenCanvas` 这个全局藏掉（`Object.defineProperty(..., {value: undefined})`），
  逼它走普通 canvas 那条路（普通 canvas 上 WebGL2 是好的）。同时页面里的能力探测**复刻它的分支**，
  别用库自带的 `isSupported()`。
- **教训**：第三方库的 `isSupported()` 只保证"它认为支持"，不保证"它真的能跑"。
  嵌入这种页面，**关键路径的能力探测要照真实分支自己写一遍**。

### 坑 5：抓不到像素，不等于没画

- **现象**：想验证"效果到底画出来没有"，抓窗口像素全是透明/全黑。
- **根因**：WebKit 一旦进入加速合成（页面里有 WebGL/canvas），内容不再落在 X 窗口的位图里，
  `XGetImage`（`Gdk.pixbuf_get_from_window`）自然抓不到。早期用同一手法验证纯 HTML 页面是**能**抓到的，
  所以很容易误判成"页面空白"。
- **修法**：用 **WebKit 自己的快照接口**：
  ```python
  view.get_snapshot(WebKit2.SnapshotRegion.VISIBLE,
                    WebKit2.SnapshotOptions.TRANSPARENT_BACKGROUND, None, done, None)
  # done 里 view.get_snapshot_finish(result) → cairo.Surface，可读像素（含 alpha）
  ```
  它渲染的是"页面真实结果"，还带 alpha——一举验证"画了什么"和"圆角外是否透明"。
- **教训**：**验证手段本身要先被验证**（拿一段最简 HTML 试一次，确认手法有效再下结论）。

### 坑 6：预览页和嵌入页共用一个组件，样式却互相污染

- **现象**：嵌入页要么带着预览页的面板，要么 body 背景把透明搞没了。
- **修法**：把**组件自己的样式**（`.pill` / `.ring` / 文字）单独拆成 `pill.css`，两个页面各自引用；
  页面级的 `html/body/#root` 规则各写各的。组件样式里需要 `box-sizing` 这类基础约定就自己声明
  （`#root` 在嵌入页没有全局 `border-box` 时，`.ring` 的边框会让它从 44px 变成 48px）。

### 坑 7：首帧延迟来自"懒启动"

- **现象**：第一次录音时，窗口 0.2s 就出现了，但内容晚约 1 秒才画上（页面在加载）。
- **取舍**：可以把浮标进程在 app 启动时就预热（甚至预加载页面），代价是那 550MB 从启动就占着。
  本次选择"懒启动 + 缓存后几帧"，只让每次 app 运行的**第一次**录音承担这个延迟。
- **修法（本次采用的）**：窗口在第一条指令到达时就建，页面加载期间来的帧**只留最后一条状态和最后一条音量**，
  页面就绪后补发——不会把整段历史灌进去，也不会丢当前状态。

---

## 4. 验证手法清单（可直接抄）

| 要验的事 | 手法 |
| --- | --- |
| 页面真的跑起来了 | 页面暴露 `debug()`，宿主 `evaluate_javascript` 读回来；注意 4.1 的 `evaluate_javascript_finish` 直接返回 `JavaScriptCore.Value`，要 `.to_string()` |
| 页面里报了什么错 | `UserScript` 在 document-start 注入收集器，监听 `error`（**capture: true**）、`unhandledrejection`，并包一层 `console.error` |
| 画了什么 / 透明不透明 | WebKit 快照接口 + `TRANSPARENT_BACKGROUND`，读 cairo surface 的 RGBA 逐像素统计 |
| 窗口合不合格 | `xwininfo -id <wid>`：`Depth` 必须是 32、`Override Redirect: yes`、`Map State: IsViewable` |
| **点击穿透** | 直接问 X 服务器 input shape：`ctypes` 调 `libXext` 的 `XShapeGetRectangles(display, wid, ShapeInput=2, ...)`，**0 块矩形 = 穿透**。一定要带一个"没设穿透"的对照窗口，否则分不清"空"和"没设" |
| 进程内确认 | `Gdk.Window.get_pass_through()`（设了能读回来）、`get_override_redirect()` 只有 setter，别指望读 |
| 开销 | 遍历 `/proc` 取整棵进程树的 `VmRSS` 与 `utime+stime`（WebKit 会另起 WebProcess / NetworkProcess，**必须按树算**） |
| 别被骗 | `pgrep -f <关键字>` 会匹配到自己这条命令；用 `[b]ridge` 这类括号技巧，或干脆在 /proc 里按片段拼字符串扫 |

---

## 5. 开销账（本次实测，供决策参考）

| 状态 | CPU | 内存 |
| --- | --- | --- |
| WebKit 浮标显示中（光束动画跑着） | ≈ **1 个核**（107–124%，主要是 WebProcess） | RSS ≈ **550MB**（含 WebProcess/NetworkProcess） |
| WebKit 浮标隐藏（页面挂载但不显示特效） | ≈ **3%** | 同上（进程常驻） |
| cairo 浮标（原路） | 基本为 0 | 基本为 0 |

- 这钱主要花在**特效本身**：实测把金属环（WebGL）关掉，CPU 反而 119%，说明大头是光束的
  模糊层——WebKit 下 `voice-glow` 的模糊走 CSS，在这台机器上是 CPU 在做。
- 能省的都省了：隐藏时把整棵特效树卸载（6.4% → 3.2%），页面里 `paused`/`active` 都跟着状态走。
- 所以务必**留一条退路**：`overlay_renderer` 一个配置项就能切回 cairo。桌面常驻工具里，
  "能一键关掉这个开销"比"效果更好看"更重要。

---

## 6. 什么时候该用、什么时候别用

**适合**：

- 这块 UI 是**局部**的、尺寸固定、生命周期短（浮标、输入法候选、录屏指示器）。
- 视觉效果在原生侧成本极高，而 Web 侧现成（这个案例：光束 / 金属环 / 液态动效）。
- 能接受 500MB 级别的常驻内存，或能做到"用完卸载"。

**别用**：

- 需要极低内存/延迟的常驻组件（那就继续原生，或者只嵌"静态图"）。
- 需要和宿主高频双向交互（每帧要往返的），JS 调用的开销和时序问题会教你做人。
- 目标机器上 WebKitGTK 不可用或版本过旧（本次遇到的能力缺口就有两个：`OffscreenCanvas` 的 WebGL2、
  `file://` 的 CORS）——**先写探针，再动手**。

---

## 7. 如果再来一次，顺序会是这样

1. **写探针**：在目标机上用一个最小页面，把"窗口能不能满足要求（不抢焦点、穿透、透明）"
   和"特效库依赖的能力有没有（WebGL2 等）"验一遍。**探针只测不集成。**
2. **定协议**：让被嵌入的页面吃和原来一模一样的帧，宿主接口一行不改。
3. **页面侧先跑通**：先把页面做成"只有一个组件、整页透明、尺寸写死"的嵌入模式，
   并在页面里暴露 `apply()` + `debug()`。
4. **宿主侧加渲染器 + 自动回退**，原实现保留。
5. **逐项验证**：快照看内容、X 侧看穿透、`/proc` 看开销、注入收集器看报错。
6. **量开销、写清退路**，把数字和开关写进 README。

---

## 附：本次涉及的代码位置

```
prototype/
  embed.html              嵌入页（整页透明、尺寸写死、藏掉 OffscreenCanvas）
  src/embed.tsx           只画胶囊的 React 入口 + window.voiceOverlay 接口 + 错误边界
  src/pill.css            预览页与嵌入页共用的胶囊样式
  src/App.tsx             预览页（带面板，用来调参）
  vite.config.ts          多入口：index.html + embed.html
  bridge.py               预览用的假数据 + 静态服务（嵌入时不用）
  webkit_probe.py         可行性探针（窗口条件 + WebGL + 开销）
doubao_voice/
  overlayd.py             cairo / WebKit 两种渲染器 + OverlayHost 自动回退
  overlay.py              主进程侧，拉起并驱动 overlayd（协议未变）
  config.py               overlay_renderer: auto | webkit | cairo
```
