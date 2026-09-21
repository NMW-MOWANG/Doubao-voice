"""系统托盘图标（Linux / StatusNotifierItem）。

程序常驻后台后需要一个不占屏幕的入口，用来打开设置、切换录音方式、退出。
图标是运行时用像素运算画出来的（不依赖任何图片资源）。

Windows 版走 Shell_NotifyIconW，Linux 这边换成 StatusNotifierItem（SNI）：

* 在会话总线上占一个自己的总线名（``org.kde.StatusNotifierItem-<pid>-1``），
  导出 ``/StatusNotifierItem``（图标、提示、点击）和 ``/MenuBar``（dbusmenu 菜单）；
* 再调用 ``org.kde.StatusNotifierWatcher.RegisterStatusNotifierItem`` 把总线名报上去，
  宿主（GNOME 顶栏的 appindicator 扩展）会自己来读属性、画图标、渲染菜单，
  点击菜单项时回调 ``com.canonical.dbusmenu.Event``。

对象导出和属性变更都排在 GTK 的默认主上下文里（``GLib.idle_add``）：主程序是单线程
的 GTK 应用，GLib/Gio 对象只在主上下文里用，没必要为托盘另开一个线程跑 D-Bus。
"""

from __future__ import annotations

import os

from gi.repository import Gio, GLib

STATE_COLORS = {
    "idle": (150, 156, 163),
    "recording": (232, 69, 60),
    "recognizing": (245, 166, 35),
    "error": (217, 48, 37),
}
STATE_TIPS = {
    "idle": "待机",
    "recording": "正在录音",
    "recognizing": "识别中",
    "error": "出错了",
}

# 宿主按自己的顶栏尺寸挑像素图，多给几档省得它拉伸
ICON_SIZES = (32, 22)
# 只在画不出像素图时才用的主题图标名（见 TrayIcon._icon_name）
THEME_ICON_NAME = "audio-input-microphone"

SNI_INTERFACE = "org.kde.StatusNotifierItem"
MENU_INTERFACE = "com.canonical.dbusmenu"
PROPERTIES_INTERFACE = "org.freedesktop.DBus.Properties"
WATCHER_BUS_NAME = "org.kde.StatusNotifierWatcher"
WATCHER_OBJECT_PATH = "/StatusNotifierWatcher"
WATCHER_INTERFACE = "org.kde.StatusNotifierWatcher"
SNI_OBJECT_PATH = "/StatusNotifierItem"
MENU_OBJECT_PATH = "/MenuBar"
SNI_ID = "doubao-voice"

SNI_XML = """
<node>
  <interface name="org.kde.StatusNotifierItem">
    <property name="Category" type="s" access="read"/>
    <property name="Id" type="s" access="read"/>
    <property name="Title" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="IconName" type="s" access="read"/>
    <property name="IconPixmap" type="a(iiay)" access="read"/>
    <property name="Menu" type="o" access="read"/>
    <property name="ItemIsMenu" type="b" access="read"/>
    <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
    <method name="Activate">
      <arg name="x" type="i" direction="in"/>
      <arg name="y" type="i" direction="in"/>
    </method>
    <method name="SecondaryActivate">
      <arg name="x" type="i" direction="in"/>
      <arg name="y" type="i" direction="in"/>
    </method>
    <method name="ContextMenu">
      <arg name="x" type="i" direction="in"/>
      <arg name="y" type="i" direction="in"/>
    </method>
    <method name="Scroll">
      <arg name="delta" type="i" direction="in"/>
      <arg name="orientation" type="s" direction="in"/>
    </method>
  </interface>
</node>
"""

MENU_XML = """
<node>
  <interface name="com.canonical.dbusmenu">
    <property name="Version" type="u" access="read"/>
    <property name="TextDirection" type="s" access="read"/>
    <property name="Status" type="s" access="read"/>
    <property name="IconThemePath" type="as" access="read"/>
    <method name="GetLayout">
      <arg name="parentId" type="i" direction="in"/>
      <arg name="recursionDepth" type="i" direction="in"/>
      <arg name="propertyNames" type="as" direction="in"/>
      <arg name="revision" type="u" direction="out"/>
      <arg name="layout" type="(ia{sv}av)" direction="out"/>
    </method>
    <method name="GetGroupProperties">
      <arg name="ids" type="ai" direction="in"/>
      <arg name="propertyNames" type="as" direction="in"/>
      <arg name="properties" type="a(ia{sv})" direction="out"/>
    </method>
    <method name="Event">
      <arg name="id" type="i" direction="in"/>
      <arg name="eventId" type="s" direction="in"/>
      <arg name="data" type="v" direction="in"/>
      <arg name="timestamp" type="u" direction="in"/>
    </method>
    <method name="AboutToShow">
      <arg name="id" type="i" direction="in"/>
      <arg name="needUpdate" type="b" direction="out"/>
    </method>
    <method name="AboutToShowGroup">
      <arg name="ids" type="ai" direction="in"/>
      <arg name="updatesNeeded" type="ai" direction="out"/>
      <arg name="idErrors" type="ai" direction="out"/>
    </method>
    <signal name="LayoutUpdated">
      <arg name="revision" type="u"/>
      <arg name="parent" type="i"/>
    </signal>
  </interface>
</node>
"""

# 菜单结构：根节点必须是 id 0（宿主就是按 0 认根的），分隔线的文字是 None。
MENU_ROOT_ID = 0
ID_MODE_HOLD, ID_MODE_TOGGLE, ID_SEPARATOR_TOP = 1, 2, 3
ID_SHOW, ID_SETTINGS, ID_SEPARATOR_BOTTOM, ID_QUIT = 4, 5, 6, 7

MENU_ITEMS = (
    (ID_MODE_HOLD, "长按说话", "mode:hold"),
    (ID_MODE_TOGGLE, "按一下开始 / 再按结束", "mode:toggle"),
    (ID_SEPARATOR_TOP, None, None),
    (ID_SHOW, "显示主界面", "show"),
    (ID_SETTINGS, "设置…", "settings"),
    (ID_SEPARATOR_BOTTOM, None, None),
    (ID_QUIT, "退出", "quit"),
)
MENU_LABELS = {item_id: label for item_id, label, _ in MENU_ITEMS if label}
MENU_ACTIONS = {item_id: action for item_id, _, action in MENU_ITEMS if action}
MENU_SEPARATORS = {item_id for item_id, label, _ in MENU_ITEMS if label is None}
# 带勾选的项：勾上表示当前是哪种触发方式
MODE_ITEMS = {ID_MODE_HOLD: "hold", ID_MODE_TOGGLE: "toggle"}


# ---------------- 图标绘制 ----------------

def _in_mic(u: float, v: float) -> bool:
    """在 32x32 设计网格里判断点是否落在麦克风图形上。"""
    if ((u - 16.0) / 5.2) ** 2 + ((v - 13.0) / 8.2) ** 2 <= 1.0:
        return True
    dx, dy = u - 16.0, v - 15.0
    radius = (dx * dx + dy * dy) ** 0.5
    if dy >= 0.5 and 7.8 <= radius <= 10.6:
        return True
    if 15.0 <= u <= 17.0 and 25.0 <= v <= 28.5:
        return True
    if 11.0 <= u <= 21.0 and 28.5 <= v <= 30.5:
        return True
    return False


def _icon_pixels(size: int, rgb: tuple[int, int, int]) -> bytes:
    """画一张 size×size 的图标，返回 ARGB32 大端（每像素 A,R,G,B）的原始字节。

    SNI 的 IconPixmap 就是这种网络序的 ARGB32；每像素在 32x32 网格里做 4x4 超采样，
    边缘靠 alpha 过渡（和 Windows 版的画法一致，只换了字节顺序）。
    """
    red, green, blue = rgb
    samples = 4
    buffer = bytearray(size * size * 4)
    step = 32.0 / size
    for y in range(size):
        for x in range(size):
            hits = 0
            for sy in range(samples):
                for sx in range(samples):
                    u = (x + (sx + 0.5) / samples) * step
                    v = (y + (sy + 0.5) / samples) * step
                    if _in_mic(u, v):
                        hits += 1
            offset = (y * size + x) * 4
            buffer[offset] = int(255 * hits / (samples * samples))
            buffer[offset + 1] = red
            buffer[offset + 2] = green
            buffer[offset + 3] = blue
    return bytes(buffer)


def _interface_info(xml: str) -> Gio.DBusInterfaceInfo:
    """把接口 XML 解析成接口信息（导出对象和 introspection 都用它）。"""
    return Gio.DBusNodeInfo.new_for_xml(xml).interfaces[0]


def _name_owner(connection: Gio.DBusConnection, name: str) -> str | None:
    """问 dbus-daemon 这个名字有没有主；没有（或服务不存在）返回 None。"""
    try:
        result = connection.call_sync(
            "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
            "GetNameOwner", GLib.Variant("(s)", (name,)),
            GLib.VariantType.new("(s)"), Gio.DBusCallFlags.NONE, 2000, None,
        )
    except GLib.Error:
        return None
    return result.unpack()[0]


# ---------------- 托盘 ----------------

class TrayIcon:
    """事件：("tray", "settings"/"show"/"quit"/"mode:hold"/"mode:toggle")"""

    def __init__(self, out_queue, tooltip: str = "豆包语音输入"):
        self.queue = out_queue
        self.tooltip = tooltip or "豆包语音输入"
        self.error: str | None = None
        self.bus_name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
        self._state = "idle"
        self._mode = "hold"
        self._connection: Gio.DBusConnection | None = None
        self._registrations: list[int] = []
        self._name_id = 0
        self._revision = 1
        self._pixmap_cache: dict[str, list[tuple[int, int, bytes]]] = {}
        self._ready = False
        self._stopped = False

    # ---------- 外部接口 ----------

    def start(self) -> None:
        """把注册动作排进主循环，构造完立刻返回。"""
        GLib.idle_add(self._setup)

    def wait_ready(self, timeout: float = 2.0) -> None:
        """跑一个小的嵌套主循环，等 start() 排进去的注册动作做完。"""
        if self._ready:
            return
        deadline = GLib.get_monotonic_time() + int(timeout * 1_000_000)
        loop = GLib.MainLoop()

        def poll() -> bool:
            if self._ready or GLib.get_monotonic_time() >= deadline:
                loop.quit()
                return GLib.SOURCE_REMOVE
            return GLib.SOURCE_CONTINUE

        GLib.timeout_add(10, poll)
        loop.run()

    def set_state(self, state: str) -> None:
        if state == self._state:
            return
        self._state = state
        GLib.idle_add(self._apply_state)

    def set_mode(self, mode: str) -> None:
        if mode == self._mode:
            return
        self._mode = mode
        GLib.idle_add(self._apply_mode)

    def stop(self) -> None:
        """摘掉图标。由主循环调用（也就是 GTK 那个线程）。"""
        self._stopped = True
        for registration in self._registrations:
            if self._connection is not None:
                self._connection.unregister_object(registration)
        self._registrations.clear()
        if self._name_id:
            # 名字一放掉，宿主就会把图标和菜单一起收走
            Gio.bus_unown_name(self._name_id)
            self._name_id = 0

    # ---------- 注册 ----------

    def _setup(self) -> bool:
        """（主循环里）导出对象、占总线名、向 watcher 报到。"""
        if self._stopped:  # start() 排进来的动作还没跑，stop() 就先来了
            return GLib.SOURCE_REMOVE
        try:
            self._connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        except GLib.Error as exc:
            self._fail(f"连不上会话总线，托盘图标用不了：{exc.message}")
            return GLib.SOURCE_REMOVE

        try:
            self._registrations.append(self._connection.register_object(
                SNI_OBJECT_PATH, _interface_info(SNI_XML),
                self._on_item_call, self._on_property_get, None,
            ))
            self._registrations.append(self._connection.register_object(
                MENU_OBJECT_PATH, _interface_info(MENU_XML),
                self._on_menu_call, self._on_property_get, None,
            ))
        except GLib.Error as exc:
            self._fail(f"导出托盘 D-Bus 对象失败：{exc.message}")
            return GLib.SOURCE_REMOVE

        self._name_id = Gio.bus_own_name_on_connection(
            self._connection, self.bus_name, Gio.BusNameOwnerFlags.REPLACE,
            self._on_name_acquired, self._on_name_lost,
        )
        return GLib.SOURCE_REMOVE

    def _on_name_acquired(self, connection: Gio.DBusConnection, name: str) -> None:
        try:
            self._register_with_watcher()
        except GLib.Error as exc:
            self.error = f"注册系统托盘失败：{exc.message}"
        finally:
            self._ready = True

    def _on_name_lost(self, connection: Gio.DBusConnection | None, name: str) -> None:
        if self._stopped:
            return
        self.error = "托盘的总线名被别的程序占走了，图标显示不出来"
        self._ready = True

    def _register_with_watcher(self) -> None:
        """把自己的总线名报给 watcher。watcher 不在就只记一句错误，不抛异常。"""
        if _name_owner(self._connection, WATCHER_BUS_NAME) is None:
            self.error = "系统里没有 StatusNotifierWatcher，顶栏放不了托盘图标"
            return
        if not self._watcher_has_host():
            # 能注册，但没人画图标；照样注册，等宿主出现时图标就出来了
            self.error = "系统托盘没有 SNI 宿主（顶栏没开 appindicator 之类的扩展），图标不会显示"
        self._connection.call_sync(
            WATCHER_BUS_NAME, WATCHER_OBJECT_PATH, WATCHER_INTERFACE,
            "RegisterStatusNotifierItem", GLib.Variant("(s)", (self.bus_name,)),
            None, Gio.DBusCallFlags.NONE, 3000, None,
        )

    def _watcher_has_host(self) -> bool:
        """watcher 有没有绑定宿主。读不到就当成有（宁可多显示图标，也别误报）。"""
        try:
            result = self._connection.call_sync(
                WATCHER_BUS_NAME, WATCHER_OBJECT_PATH, PROPERTIES_INTERFACE, "Get",
                GLib.Variant("(ss)", (WATCHER_INTERFACE, "IsStatusNotifierHostRegistered")),
                GLib.VariantType.new("(v)"), Gio.DBusCallFlags.NONE, 2000, None,
            )
        except GLib.Error:
            return True
        value = result.unpack()[0]
        return bool(value.unpack() if isinstance(value, GLib.Variant) else value)

    def _fail(self, message: str) -> None:
        self.error = message
        self._ready = True

    # ---------- 属性 ----------

    def _on_property_get(self, connection, sender, path, interface, name):
        if interface == SNI_INTERFACE:
            return self._item_property(name)
        if interface == MENU_INTERFACE:
            return self._menu_property(name)
        return None

    def _item_property(self, name: str):
        if name == "Category":
            return GLib.Variant("s", "ApplicationStatus")
        if name == "Id":
            return GLib.Variant("s", SNI_ID)
        if name == "Title":
            return GLib.Variant("s", self.tooltip)
        if name == "Status":
            return GLib.Variant("s", "Active")
        if name == "IconName":
            return GLib.Variant("s", self._icon_name())
        if name == "IconPixmap":
            return GLib.Variant("a(iiay)", self._pixmaps())
        if name == "Menu":
            return GLib.Variant("o", MENU_OBJECT_PATH)
        if name == "ItemIsMenu":
            return GLib.Variant("b", False)
        if name == "ToolTip":
            return self._tooltip_variant()
        return None

    def _menu_property(self, name: str):
        if name == "Version":
            return GLib.Variant("u", 3)
        if name == "TextDirection":
            return GLib.Variant("s", "ltr")
        if name == "Status":
            return GLib.Variant("s", "normal")
        if name == "IconThemePath":
            return GLib.Variant("as", [])
        return None

    def _icon_name(self) -> str:
        """主题图标名，只在画不出像素图时兜底。

        SNI 规定名字优先于像素图，GNOME 的 appindicator 扩展也是这么实现的
        （appIndicator.js 的 _createIcon：名字查到了就直接用它，根本不看
        IconPixmap），名字一旦非空，状态颜色就永远不会显示，所以这里留空。
        """
        return "" if self._pixmaps() else THEME_ICON_NAME

    def _tooltip_variant(self) -> GLib.Variant:
        return GLib.Variant(
            "(sa(iiay)ss)",
            ("", self._pixmaps(), self.tooltip, STATE_TIPS.get(self._state, "")),
        )

    def _pixmaps(self) -> list[tuple[int, int, bytes]]:
        """当前状态的图标像素图（按状态缓存，画一次几十毫秒）。"""
        if self._state not in self._pixmap_cache:
            color = STATE_COLORS.get(self._state, STATE_COLORS["idle"])
            self._pixmap_cache[self._state] = [
                (size, size, _icon_pixels(size, color)) for size in ICON_SIZES
            ]
        return self._pixmap_cache[self._state]

    # ---------- 图标点击 ----------

    def _on_item_call(self, connection, sender, path, interface, method, parameters, invocation):
        if method == "Activate":
            self.queue.put(("tray", "show"))
        elif method == "SecondaryActivate":
            self.queue.put(("tray", "settings"))
        # ContextMenu：菜单由宿主自己渲染，这里不用动
        # Scroll：没有可滚的东西，忽略
        invocation.return_value(None)

    # ---------- 菜单 ----------

    def _on_menu_call(self, connection, sender, path, interface, method, parameters, invocation):
        if method == "GetLayout":
            invocation.return_value(self._layout_variant())
        elif method == "GetGroupProperties":
            ids, _names = parameters.unpack()
            invocation.return_value(GLib.Variant.new_tuple(GLib.Variant(
                "a(ia{sv})",
                [(item_id, self._item_properties(item_id)) for item_id in ids],
            )))
        elif method == "Event":
            item_id, event_id, _data, _timestamp = parameters.unpack()
            self._on_menu_event(item_id, event_id)
            invocation.return_value(None)
        elif method == "AboutToShow":
            # 菜单一直是新的（状态变化时我们自己发 LayoutUpdated），不用宿主再刷
            invocation.return_value(GLib.Variant("(b)", (False,)))
        else:  # AboutToShowGroup
            invocation.return_value(GLib.Variant.new_tuple(
                GLib.Variant("ai", []), GLib.Variant("ai", []),
            ))

    def _layout_variant(self) -> GLib.Variant:
        """GetLayout 的返回值。根节点的 id 必须是 0（宿主按 id 0 认根）。"""
        children = [self._item_variant(item_id, []) for item_id, _, _ in MENU_ITEMS]
        return GLib.Variant.new_tuple(
            GLib.Variant("u", self._revision),
            self._item_variant(MENU_ROOT_ID, children),
        )

    def _item_variant(self, item_id: int, children: list[GLib.Variant]) -> GLib.Variant:
        """一个菜单节点的 (id, 属性, 子节点) 结构。"""
        return GLib.Variant.new_tuple(
            GLib.Variant("i", item_id),
            GLib.Variant("a{sv}", self._item_properties(item_id)),
            GLib.Variant("av", children),
        )

    def _item_properties(self, item_id: int) -> dict[str, GLib.Variant]:
        """一个菜单项的 dbusmenu 属性；勾选状态跟着 self._mode 走。"""
        if item_id == MENU_ROOT_ID:
            return {"children-display": GLib.Variant("s", "submenu")}
        if item_id in MENU_SEPARATORS:
            return {
                "type": GLib.Variant("s", "separator"),
                "visible": GLib.Variant("b", True),
            }
        label = MENU_LABELS.get(item_id)
        if label is None:
            return {}
        properties = {
            "label": GLib.Variant("s", label),
            "enabled": GLib.Variant("b", True),
            "visible": GLib.Variant("b", True),
            "type": GLib.Variant("s", "standard"),
        }
        if item_id in MODE_ITEMS:
            properties["toggle-type"] = GLib.Variant("s", "checkmark")
            checked = self._mode == MODE_ITEMS[item_id]
            properties["toggle-state"] = GLib.Variant("i", 1 if checked else 0)
        return properties

    def _on_menu_event(self, item_id: int, event_id: str) -> None:
        if event_id != "clicked":
            return
        action = MENU_ACTIONS.get(item_id)
        if action:
            self.queue.put(("tray", action))

    # ---------- 变更通知 ----------

    def _apply_state(self) -> bool:
        self._notify_changed({
            "IconName": GLib.Variant("s", self._icon_name()),
            "IconPixmap": GLib.Variant("a(iiay)", self._pixmaps()),
            "ToolTip": self._tooltip_variant(),
            "Status": GLib.Variant("s", "Active"),
        })
        return GLib.SOURCE_REMOVE

    def _apply_mode(self) -> bool:
        if self._stopped or self._connection is None:
            return GLib.SOURCE_REMOVE
        self._revision += 1
        self._connection.emit_signal(
            None, MENU_OBJECT_PATH, MENU_INTERFACE, "LayoutUpdated",
            GLib.Variant("(ui)", (self._revision, MENU_ROOT_ID)),
        )
        return GLib.SOURCE_REMOVE

    def _notify_changed(self, changed: dict[str, GLib.Variant]) -> None:
        """宿主靠 PropertiesChanged 知道该重画图标了。"""
        if self._stopped or self._connection is None:
            return
        self._connection.emit_signal(
            None, SNI_OBJECT_PATH, PROPERTIES_INTERFACE, "PropertiesChanged",
            GLib.Variant("(sa{sv}as)", (SNI_INTERFACE, changed, [])),
        )
