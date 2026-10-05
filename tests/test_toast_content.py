"""Toast 内容元素 + 按钮激活 + 小黑条渲染的回归测试。

    python tests/test_toast_content.py

覆盖三块：

1. **解析**（``features/catch_notify/toast.py``）：文本角色（标题 / 正文 / 归属）、
   旧模板的 ``id``、图片、按钮、输入框、header / audio / progress / scenario，
   以及各种畸形 XML（未闭合、单引号属性、CDATA、字节 payload、纯垃圾）。
2. **激活**（``features/catch_notify/activation.py``）：AUMID → CLSID →
   ``CoCreateInstance`` → ``Activate`` 的规划与执行，包括用自造 vtable 验证
   ctypes 参数封送（不会真的拉起任何应用）。
3. **显示**（``features/XHT/Lib/Notify.py``）：按内容元素渲染、图片尺寸、
   按钮链接 → 信号 → 激活 → 反馈的完整链路。

不需要真实通知库，也不改动系统状态；唯一会读系统的是「注册表只读一致性」那一项，
找不到样例时会标记 SKIP。
"""

import ctypes
import os
import shutil
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402 - 需要在 sys.path 之后

#: 整个测试期间都要活着的 QApplication。放在模块级：如果只由某个测试函数里的局部变量
#: 持有，那个函数一返回 QApplication 就被回收，后面的测试再建 QWidget 会直接
#: ``QWidget: Must construct a QApplication before a QWidget`` 把进程带走（没有回溯）。
app = QApplication.instance() or QApplication(sys.argv)

failures = []


def scratch_dir(prefix: str) -> str:
    """要一个真的能写的临时目录。

    正常环境下就是系统临时目录；在受限沙箱里新建的子目录可能不可写，那就退回
    项目目录下的 ``_scratch_*``（测试结束会删掉）。
    """
    try:
        path = tempfile.mkdtemp(prefix=prefix)
        probe = os.path.join(path, ".probe")
        with open(probe, "w", encoding="utf-8") as handle:
            handle.write("x")
        os.remove(probe)
        return path
    except OSError:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_scratch_" + prefix)
        os.makedirs(path, exist_ok=True)
        return path


def check(name, ok, detail=""):
    mark = "SKIP" if ok is None else ("PASS" if ok else "FAIL")
    print(("[%s] " % mark) + name + (("  " + str(detail)) if detail else ""))
    if ok is False:
        failures.append(name)


# --------------------------------------------------------------------------- #
# 样例 XML
# --------------------------------------------------------------------------- #
RICH_XML = """<?xml version="1.0" encoding="utf-8"?>
<toast launch="action=open&amp;id=42" scenario="reminder" duration="long"
       activationType="foreground" displayTimestamp="2026-10-02T20:00:00Z">
  <visual>
    <binding template="ToastGeneric">
      <text>小明的消息</text>
      <text>晚上一起吃饭吗？</text>
      <text>老地方见</text>
      <text placement="attribution">来自 微信</text>
      <image placement="hero" src="C:\\tmp\\hero.png" alt="横幅"/>
      <image placement="appLogoOverride" hint-crop="circle" src="file:///C:/tmp/logo.png"/>
      <image src="https://example.com/inline.png"/>
      <progress title="下载" status="进行中" value="0.4"/>
    </binding>
  </visual>
  <actions>
    <input id="reply" type="text" placeHolderContent="回复…" defaultInput="好"/>
    <input id="pick" type="selection" defaultInput="2">
      <selection id="1" content="选项一"/>
      <selection id="2" content="选项二"/>
    </input>
    <action content="回复" arguments="action=reply" hint-inputId="reply" hint-buttonStyle="Success"/>
    <action content="打开" arguments="action=open" placement="contextMenu"/>
    <action content="忽略" arguments="dismiss" activationType="system"/>
    <action content="打开网页" arguments="https://example.com" activationType="protocol"/>
  </actions>
  <audio src="ms-winsoundevent:Notification.IM" silent="true"/>
  <header id="h" title="提醒" subtitle="今天 20:00" arguments="action=header"/>
</toast>"""

LEGACY_XML = ('<toast><visual><binding template="ToastImageAndText02">'
              '<text id="2">正文在文档里排在前面</text>'
              '<text id="1">标题</text>'
              '<image id="1" src="C:\\tmp\\legacy.png"/>'
              '</binding></visual></toast>')

MULTI_BINDING_XML = ('<toast><visual>'
                     '<binding template="ToastText01"><text>英文标题</text></binding>'
                     '<binding template="ToastGeneric"><text>中文标题</text>'
                     '<text>中文正文</text></binding>'
                     '</visual></toast>')


# --------------------------------------------------------------------------- #
# 1) 解析
# --------------------------------------------------------------------------- #
def test_parsing():
    print("=== 1) Toast 内容元素解析 ===")
    from features.catch_notify.records import Notification, extract_texts
    from features.catch_notify.toast import (
        PLACEMENT_APP_LOGO, PLACEMENT_HERO, ROLE_ATTRIBUTION, ROLE_BODY, ROLE_TITLE,
        local_image_path, parse_toast,
    )

    content = parse_toast(RICH_XML)
    check("模板解析", content.template == "ToastGeneric", content.template)
    check("场景解析", content.scenario == "reminder" and content.is_reminder,
          content.scenario)
    check("launch 解析（实体已解）", content.launch == "action=open&id=42", content.launch)
    check("时长 / 时间戳", content.duration == "long"
          and content.display_timestamp == "2026-10-02T20:00:00Z")
    check("标题角色", content.title == "小明的消息", content.title)
    check("正文多行（归属不算正文）",
          content.body_lines == ("晚上一起吃饭吗？", "老地方见"), content.body_lines)
    check("归属文本", content.attribution == "来自 微信", content.attribution)
    check("文本角色序列",
          tuple(text.role for text in content.texts)
          == (ROLE_TITLE, ROLE_BODY, ROLE_BODY, ROLE_ATTRIBUTION))
    check("No parse errors", content.parse_errors == (), content.parse_errors)

    check("图片数量", len(content.images) == 3, [i.placement for i in content.images])
    check("hero 图片", content.hero_image is not None
          and content.hero_image.placement == PLACEMENT_HERO)
    check("应用图标", content.app_logo is not None
          and content.app_logo.placement == PLACEMENT_APP_LOGO
          and content.app_logo.hint_crop == "circle")
    check("内联图片只有网络那条", [i.src for i in content.inline_images]
          == ["https://example.com/inline.png"], [i.src for i in content.inline_images])
    check("本地路径解析", content.hero_image.path() == os.path.join("C:\\", "tmp", "hero.png"),
          content.hero_image.path())
    check("file:/// 路径解析",
          content.app_logo.path() == os.path.join("C:\\", "tmp", "logo.png"),
          content.app_logo.path())
    check("网络图片没有本地路径", content.inline_images[0].path() is None)

    check("按钮不再是链接而是激活请求",
          [a.content for a in content.button_actions] == ["回复", "忽略", "打开网页"],
          [a.content for a in content.button_actions])
    check("上下文菜单项单独归类",
          [a.content for a in content.context_menu_actions] == ["打开"], 
          [a.content for a in content.context_menu_actions])
    reply, ignore, protocol = content.button_actions
    check("按钮 arguments / hint-inputId",
          reply.arguments == "action=reply" and reply.hint_input_id == "reply"
          and reply.hint_button_style == "Success", reply.to_dict())
    check("系统动作识别", ignore.is_system and not reply.is_system)
    check("协议动作识别", protocol.is_protocol and protocol.arguments == "https://example.com")

    check("输入框（文本 + 下拉）",
          [item.id for item in content.inputs] == ["reply", "pick"], content.inputs)
    pick = content.input("pick")
    check("下拉选项解析",
          pick is not None and [c.content for c in pick.choices] == ["选项一", "选项二"]
          and pick.default_input == "2", pick.to_dict() if pick else None)
    check("按钮收集输入：hint-inputId 指定",
          [item.id for item in content.action_inputs(reply)] == ["reply"])
    check("系统动作 / 协议动作不带输入",
          content.action_inputs(ignore) == () and content.action_inputs(protocol) == ())

    header = content.header
    check("header 解析", header is not None and header.label == "提醒 · 今天 20:00"
          and header.arguments == "action=header", header.to_dict() if header else None)
    check("audio 静音", content.is_silent and content.audio.src.endswith("Notification.IM"))
    check("progress 解析", content.progress is not None
          and content.progress.label == "下载 · 进行中 · 0.4",
          content.progress.label if content.progress else None)

    # 旧模板：id 决定角色，而不是文档顺序
    legacy = parse_toast(LEGACY_XML)
    check("旧模板按 id 取标题", legacy.title == "标题", legacy.title)
    check("旧模板正文", legacy.body_lines == ("正文在文档里排在前面",), legacy.body_lines)
    check("旧模板图片", len(legacy.images) == 1 and legacy.images[0].id == "1")

    # 多 binding：主内容取 ToastGeneric，但所有文本都要保留
    multi = parse_toast(MULTI_BINDING_XML)
    check("多 binding 优先 ToastGeneric", multi.title == "中文标题", multi.title)
    check("多 binding 保留全部文本",
          [t.content for t in multi.all_texts] == ["英文标题", "中文标题", "中文正文"],
          [t.content for t in multi.all_texts])
    check("binding 列表", len(multi.bindings) == 2, len(multi.bindings))

    # extract_texts 的旧行为（文档顺序、解实体、去空）保持不变
    check("extract_texts 兼容", extract_texts(RICH_XML)
          == ("小明的消息", "晚上一起吃饭吗？", "老地方见", "来自 微信"),
          extract_texts(RICH_XML))
    check("extract_texts 单标签片段",
          extract_texts('<text hint-style="base">A</text>') == ("A",))

    # 畸形 / 边界输入
    check("空值不炸", parse_toast(None).title == "" and parse_toast("").to_dict()["texts"] == [])
    check("非 XML 垃圾不炸", parse_toast("not xml at all").title == "")
    check("未闭合标签仍取出文本",
          parse_toast('<toast><visual><binding template="ToastGeneric">'
                      '<text>未闭合 & <b>X</b>').title == "未闭合 & X")
    check("单引号属性",
          parse_toast("<text placement='attribution'>归属</text>").attribution == "归属")
    check("CDATA 内容",
          parse_toast('<text><![CDATA[<b>粗</b> & 号]]></text>').texts[0].content
          == "<b>粗</b> & 号")
    check("注释被忽略",
          parse_toast('<toast><!-- <text>假的</text> --><text>真的</text></toast>').title
          == "真的")
    utf16 = ('<toast><visual><binding template="ToastGeneric"><text>十六进制</text>'
             '</binding></visual></toast>').encode("utf-16")
    check("UTF-16 字节 payload", parse_toast(utf16).title == "十六进制")

    # 路径解析
    check("file:/// 百分号转义",
          local_image_path("file:///C:/a%20b/x.png") == os.path.join("C:\\", "a b", "x.png"))
    check("UNC 路径保留",
          local_image_path("file://server/share/x.png").startswith("\\\\server\\share"),
          local_image_path("file://server/share/x.png"))
    check("ms-appdata 需要包族名",
          local_image_path("ms-appdata:///local/x.png") is None
          and local_image_path("ms-appdata:///local/x.png", "Pkg_x").endswith("x.png"))
    check("ms-appx / 相对路径拒绝",
          local_image_path("ms-appx:///Assets/x.png") is None
          and local_image_path("assets/x.png") is None)

    # Notification 集成
    item = Notification(id=7, app_id="WeChat", xml=RICH_XML, texts=extract_texts(RICH_XML))
    check("Notification.title 走内容元素", item.title == "小明的消息", item.title)
    check("Notification.body 多行合并",
          item.body == "晚上一起吃饭吗？\n老地方见", item.body)
    check("Notification.other_texts 含归属",
          item.other_texts == ("老地方见", "来自 微信"), item.other_texts)
    check("Notification.text_roles",
          item.text_roles == ("title", "body", "body", "attribution"), item.text_roles)
    check("content 缓存（同一对象）", item.content is item.content)
    check("actions / inputs 便捷属性",
          len(item.actions) == 4 and len(item.inputs) == 2 and item.has_actions
          and item.has_inputs and item.attribution == "来自 微信")
    check("to_dict 带内容元素",
          item.to_dict()["content"]["title"] == "小明的消息"
          and item.to_dict()["content"]["actions"][0]["arguments"] == "action=reply")
    plain = Notification(id=8, app_id="X", xml="", texts=("甲", "乙", "丙"))
    check("无 XML 时退回 texts", plain.title == "甲" and plain.body == "乙"
          and plain.other_texts == ("丙",), (plain.title, plain.body, plain.other_texts))


# --------------------------------------------------------------------------- #
# 2) 激活
# --------------------------------------------------------------------------- #
class FakeLookup:
    """注册表查询替身。"""

    def __init__(self, clsid="", command=""):
        self.clsid = clsid
        self.command = command
        self.asked = []

    def clsid_for(self, aumid):
        self.asked.append(aumid)
        return self.clsid if aumid else ""

    def server_command(self, clsid):
        return self.command


class FakeComCaller:
    def __init__(self, code=0, detail="S_OK"):
        self.code = code
        self.detail = detail
        self.seen = None

    def call(self, clsid, aumid, arguments, inputs):
        self.seen = (clsid, aumid, arguments, tuple(inputs))
        return self.code, self.detail


def test_activation_plan():
    print("\n=== 2) 激活规划与执行 ===")
    from features.catch_notify.activation import (
        STRATEGY_ACTIVATE_APP, STRATEGY_COM, STRATEGY_DISMISS, STRATEGY_PROTOCOL,
        STRATEGY_SHELL_APPS, STRATEGY_UNSUPPORTED, aumid_candidates, build_plan,
        hresult_message, perform,
    )
    from features.catch_notify.records import Notification, extract_texts
    from features.catch_notify.toast import ToastAction, parse_toast

    item = Notification(id=1, app_id="WeChat.App", xml=RICH_XML,
                        texts=extract_texts(RICH_XML))
    content = item.content
    reply, ignore, protocol = content.button_actions

    check("AUMID 候选写法覆盖斜杠与大小写",
          set(aumid_candidates("A\\b.EXE")) == {"A\\b.EXE", "a\\b.exe", "A/b.EXE", "a/b.exe"},
          aumid_candidates("A\\b.EXE"))
    check("空 AUMID 没有候选", aumid_candidates("") == () and aumid_candidates(None) == ())

    lookup = FakeLookup("{11111111-2222-3333-4444-555555555555}",
                        '"C:\\app\\x.exe" -ToastActivated')
    plan = build_plan(item, action=reply, inputs={"reply": "好的"}, lookup=lookup)
    check("有激活器时走 COM",
          plan.strategy == STRATEGY_COM and plan.clsid.startswith("{11111111")
          and plan.executable, plan.to_dict())
    check("计划带上 arguments 与输入",
          plan.request.arguments == "action=reply"
          and plan.request.inputs == (("reply", "好的"),), plan.request.to_dict())
    check("计划带上 LocalServer32 命令行",
          plan.server_command == '"C:\\app\\x.exe" -ToastActivated')

    check("系统动作走关闭",
          build_plan(item, action=ignore, lookup=lookup).strategy == STRATEGY_DISMISS)
    check("协议动作走 ShellExecute",
          build_plan(item, action=protocol, lookup=lookup).strategy == STRATEGY_PROTOCOL)

    no_lookup = FakeLookup()
    check("没有激活器的前台按钮 → 尽力拉起应用",
          build_plan(item, action=reply, lookup=no_lookup).strategy == STRATEGY_ACTIVATE_APP,
          build_plan(item, action=reply, lookup=no_lookup).to_dict())
    body_plan = build_plan(item, lookup=no_lookup)
    check("通知本体没有激活器 → 按 AUMID 拉起并带 launch 参数",
          body_plan.strategy == STRATEGY_ACTIVATE_APP
          and body_plan.request.arguments == "action=open&id=42"
          and not body_plan.exact, body_plan.to_dict())
    check("通知本体有激活器时仍走 COM",
          build_plan(item, lookup=lookup).strategy == STRATEGY_COM)
    check("缺 AUMID 时不支持",
          build_plan(Notification(id=2, xml=RICH_XML), lookup=lookup).strategy
          == STRATEGY_UNSUPPORTED)
    check("后台动作没有激活器时明确不支持",
          build_plan(item, action=ToastAction(content="暂停", arguments="action=pause",
                                              activation_type="background"),
                     lookup=no_lookup).strategy == STRATEGY_UNSUPPORTED)
    check("精确送达标记", build_plan(item, action=reply, lookup=lookup).exact
          and not build_plan(item, lookup=no_lookup).exact)

    # 执行：全注入，不碰真实系统
    dismissed = []
    caller = FakeComCaller()
    result = perform(plan, item=item, com_caller=caller,
                     dismisser=lambda obj: (dismissed.append(obj) or (True, "已移除")))
    check("COM 激活成功", result.ok and result.method == STRATEGY_COM, result.to_dict())
    check("参数按顺序传给激活器",
          caller.seen == (plan.clsid, "WeChat.App", "action=reply", (("reply", "好的"),)),
          caller.seen)
    check("激活后顺手移除通知中心里的那条",
          result.dismissed and len(dismissed) == 1)

    dismissed.clear()
    failing = FakeComCaller(-2147467262, "E_NOINTERFACE")
    bad = perform(plan, item=item, com_caller=failing, dismisser=lambda obj: (True, ""))
    check("COM 失败时报告 HRESULT",
          (not bad.ok) and "接口" in bad.error and bad.hresult == -2147467262,
          bad.to_dict())
    check("失败时不移除通知", dismissed == [])

    launcher_calls = []

    class FakeLauncher:
        def open_protocol(self, arguments):
            launcher_calls.append(("protocol", arguments))
            return True, "ok"

        def open_apps_folder(self, aumid):
            launcher_calls.append(("apps", aumid))
            return True, "ok"

    protocol_plan = build_plan(item, action=protocol, lookup=lookup)
    ok = perform(protocol_plan, item=item, launcher=FakeLauncher(),
                 dismisser=lambda obj: (False, "没有 winsdk"))
    check("协议动作调用 ShellExecute", ok.ok and launcher_calls == [("protocol", "https://example.com")],
          launcher_calls)
    check("移除失败不影响激活成功", ok.ok and not ok.dismissed)

    body_plan = build_plan(item, lookup=no_lookup)
    launcher_calls.clear()
    app_calls = []

    class FakeAppActivator:
        def __init__(self, ok=True, detail="已拉起"):
            self.ok = ok
            self.detail = detail

        def activate(self, aumid, arguments=""):
            app_calls.append((aumid, arguments))
            return self.ok, self.detail

    activated = perform(body_plan, item=item, launcher=FakeLauncher(),
                        app_activator=FakeAppActivator(), dismiss_after=False)
    check("打包应用降级到 ActivateApplication（带 arguments）",
          activated.ok and app_calls == [("WeChat.App", "action=open&id=42")]
          and activated.method == STRATEGY_ACTIVATE_APP, (app_calls, activated.to_dict()))

    launcher_calls.clear()
    app_calls.clear()
    fallback = perform(body_plan, item=item, launcher=FakeLauncher(),
                       app_activator=FakeAppActivator(False, "没这个应用"),
                       dismiss_after=False)
    check("ActivateApplication 失败后仍退回 shell:AppsFolder",
          fallback.ok and fallback.method == STRATEGY_SHELL_APPS
          and launcher_calls == [("apps", "WeChat.App")], (launcher_calls, fallback.to_dict()))

    dismissed.clear()
    dismiss_plan = build_plan(item, action=ignore, lookup=lookup)
    perform(dismiss_plan, item=item, dismisser=lambda obj: (dismissed.append(obj) or (True, "已移除")))
    check("系统动作 = 只移除", len(dismissed) == 1 and dismissed[0] is item)

    check("HRESULT 人话", hresult_message(0).startswith("成功")
          and "没有注册" in hresult_message(-2147221164)
          and hresult_message(-12345).startswith("HRESULT 0x"))

    # 移除通知：三条通道依次尝试，成功即停
    from features.catch_notify import activation as A

    saved = (A._remove_via_listener, A._remove_via_winsdk, A._remove_via_bridge)
    attempts = []
    A._remove_via_listener = lambda obj: (attempts.append("listener") or (False, "需要授权"))
    A._remove_via_winsdk = lambda app, tag, group: (attempts.append("history") or (False, "没有包标识"))
    A._remove_via_bridge = lambda app, tag, group: (attempts.append("bridge") or (True, "已移除"))
    try:
        ok, detail = A.dismiss_from_history(item)
        check("移除通知会依次尝试三条通道",
              ok and attempts == ["listener", "history", "bridge"], (attempts, detail))
        attempts.clear()
        A._remove_via_listener = lambda obj: (attempts.append("listener") or (True, "已移除"))
        ok, detail = A.dismiss_from_history(item)
        check("第一条通道成功就不再往下试", ok and attempts == ["listener"], (attempts, detail))
    finally:
        A._remove_via_listener, A._remove_via_winsdk, A._remove_via_bridge = saved


def test_com_marshalling():
    """用自造 vtable 验证 ctypes 的 Activate 调用（不拉起任何应用）。"""
    print("\n=== 3) COM 调用封送（自造 vtable）===")
    from features.catch_notify import activation as A

    check("Activate 在 IUnknown 之后的 slot 3", A._SLOT_ACTIVATE == 3)

    calls = []
    # 自造的 vtable / 接口内存必须一直活着，否则读到的是已经释放的内存
    keep_alive = []

    @ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_wchar_p,
                        ctypes.c_wchar_p, ctypes.POINTER(A._UserInputData), ctypes.c_ulong)
    def fake_activate(this, aumid, arguments, data, count):
        values = [(data[i].Key, data[i].Value) for i in range(count)] if count else []
        calls.append((aumid, arguments, count, values))
        return 0

    def make_interface(function):
        vtable = (ctypes.c_void_p * 4)()
        vtable[A._SLOT_ACTIVATE] = ctypes.cast(function, ctypes.c_void_p).value
        # 接口指针指向的内存里放着 vtable 的地址
        slot = ctypes.c_void_p(ctypes.addressof(vtable))
        keep_alive.extend([vtable, slot])
        return ctypes.c_void_p(ctypes.addressof(slot))

    code = A.invoke_activate_callback(make_interface(fake_activate), "AUMID.X", "action=reply",
                                      [("reply", "你好"), ("pick", "2")])
    check("Activate 返回值透传", code == 0, code)
    check("参数与输入数组正确封送",
          calls == [("AUMID.X", "action=reply", 2, [("reply", "你好"), ("pick", "2")])],
          calls)

    calls.clear()
    A.invoke_activate_callback(make_interface(fake_activate), "A", "", [])
    check("没有输入时传 NULL 与 count=0", calls == [("A", "", 0, [])], calls)

    @ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_wchar_p,
                        ctypes.c_wchar_p, ctypes.POINTER(A._UserInputData), ctypes.c_ulong)
    def failing_activate(this, aumid, arguments, data, count):
        return -2147467262

    check("失败 HRESULT 原样返回",
          A.invoke_activate_callback(make_interface(failing_activate), "A", "x", [])
          == -2147467262)

    try:
        A.invoke_activate_callback(None, "A", "x", [])
        raised = False
    except Exception:  # noqa: BLE001
        raised = True
    check("空接口指针明确报错", raised)

    # IApplicationActivationManager::ActivateApplication（打包应用降级通道）
    app_calls = []

    @ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p,
                        ctypes.c_int, ctypes.POINTER(ctypes.c_ulong))
    def fake_activate_app(this, aumid, arguments, options, process_id):
        app_calls.append((aumid, arguments, options))
        process_id[0] = 4321
        return 0

    code, process_id = A.invoke_activate_application(make_interface(fake_activate_app),
                                                    "Pkg!App", "action=open", 0)
    check("ActivateApplication 参数与进程号回传",
          code == 0 and process_id == 4321
          and app_calls == [("Pkg!App", "action=open", 0)], (code, process_id, app_calls))


def test_registry_lookup():
    print("\n=== 4) 注册表查询 ===")
    from features.catch_notify import activation as A

    # 注入假的读取实现：不动真实注册表
    table = {
        (0x80000001, r"Software\Classes\AppUserModelId\Demo.App", "CustomActivator"):
            "{AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE}",
        (0x80000001, r"Software\Classes\CLSID\{AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE}\LocalServer32", None):
            '"C:\\demo\\demo.exe" -ToastActivated',
    }
    # 真实注册表的路径与值名都不区分大小写，替身也照这个来
    lowered = {(hive, path.lower(), name.lower() if name else name): value
               for (hive, path, name), value in table.items()}
    real = A._read_registry_default
    A._read_registry_default = lambda hive, path, name=None: lowered.get(
        (hive, str(path).lower(), name.lower() if name else name))
    try:
        lookup = A.RegistryActivatorLookup()
        check("按 AUMID 找到 CLSID",
              lookup.clsid_for("Demo.App") == "{AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE}",
              lookup.clsid_for("Demo.App"))
        check("大小写/斜杠变体也能命中", lookup.clsid_for("demo.app") == "{AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE}")
        check("LocalServer32 命令行", lookup.server_command("{AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE}")
              == '"C:\\demo\\demo.exe" -ToastActivated')
        check("未登记的 AUMID 返回空", lookup.clsid_for("No.Such.App") == "")
        check("非法 CLSID 不查注册表",
              lookup.server_command("not-a-guid") == "" and lookup.server_command("") == "")
        check("describe() 汇总", lookup.describe("Demo.App")["clsid"].startswith("{AAAA"))
    finally:
        A._read_registry_default = real

    # 只读一致性：拿真实注册表里的样例对一下（没有样例就跳过）
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Classes\AppUserModelId") as key:
            names = []
            index = 0
            while True:
                try:
                    names.append(winreg.EnumKey(key, index))
                except OSError:
                    break
                index += 1
            samples = []
            for name in names:
                try:
                    with winreg.OpenKey(key, name) as sub:
                        value, _ = winreg.QueryValueEx(sub, "CustomActivator")
                        if value:
                            samples.append((name, value))
                except OSError:
                    continue
    except OSError:
        samples = []

    if not samples:
        check("真实注册表一致性", None, "本机 HKCU\\Software\\Classes\\AppUserModelId 里没有 CustomActivator")
    else:
        aumid, expected = samples[0]
        found = A.default_lookup().clsid_for(aumid)
        check("真实注册表一致性", found.lower() == expected.lower(),
              "%s → %s（期望 %s）" % (aumid, found, expected))


# --------------------------------------------------------------------------- #
# 5) 显示层
# --------------------------------------------------------------------------- #
class FakeWindow(QWidget):
    """``NotificationPresenter`` 只用到这几个窗口能力。"""

    def __init__(self):
        super().__init__()
        self.is_hidden = False
        self.time_visible = True

    def set_time_visible(self, visible):
        self.time_visible = visible

    def ShowWindow(self):
        pass

    def HideWindow(self):
        pass

    def AutoSetSize(self):
        pass


class FakeResult:
    def __init__(self, ok=True, message="已发送", method="com"):
        self.ok = ok
        self.message = message
        self.method = method
        self.detail = message
        self.error = "" if ok else message
        self.hresult = 0
        self.dismissed = False


class NullLogger:
    """XHT 窗口要一个 logger，测试里用空实现。"""

    def debug(self, *args, **kwargs):
        pass

    info = warning = error = critical = debug


def pump(app, seconds=2.0):
    deadline = time.time() + seconds
    while time.time() < deadline:
        app.processEvents()
        time.sleep(0.02)


def test_display():
    print("\n=== 5) 小黑条渲染与点击 ===")
    from PySide6.QtGui import QImage

    from features.XHT.Lib.Notify import (
        ACTION_SCHEME, CENTER_SCHEME, NotificationBadge, NotificationPresenter, image_html,
    )
    from features.catch_notify.records import Notification, extract_texts

    workdir = scratch_dir("xht-toast-test-")
    hero_path = os.path.join(workdir, "hero.png")
    logo_path = os.path.join(workdir, "logo.png")
    for path, size in ((hero_path, (400, 200)), (logo_path, (64, 64))):
        image = QImage(size[0], size[1], QImage.Format.Format_ARGB32)
        image.fill(0xFF39C5BB)
        image.save(path)

    check("图片缺失时不输出 <img>",
          image_html(os.path.join(workdir, "nope.png"), max_width=40, max_height=40) == "")
    scaled = image_html(hero_path, max_width=320, max_height=90)
    check("图片等比缩放（400×200 → 180×90）",
          'width="180"' in scaled and 'height="90"' in scaled, scaled)
    logo_scaled = image_html(logo_path, max_width=20, max_height=20)
    check("小图也缩到上限内", 'width="20"' in logo_scaled and 'height="20"' in logo_scaled,
          logo_scaled)

    item = Notification(id=1, app_id="WeChat", app_name="微信", xml=RICH_XML,
                        texts=extract_texts(RICH_XML))
    badge = NotificationBadge(None)
    badge.show_content(item.content, unread=3, app_name="微信")
    text = badge.text()
    check("标题进入渲染", "小明的消息" in text)
    check("多行正文都渲染", "晚上一起吃饭吗？" in text and "老地方见" in text)
    check("归属文本渲染", "来自 微信" in text)
    check("场景 / 头部 / 进度渲染",
          "提醒" in text and "今天 20:00" in text and "进度" in text, text)
    check("未读数渲染", "🔔3" in text)
    check("按钮渲染成链接",
          'href="%s0"' % ACTION_SCHEME in text and 'href="%s1"' % ACTION_SCHEME in text
          and ">回复<" in text, text)
    check("上下文菜单项不进链接", ">打开<" not in text.replace(">打开网页<", ""))
    check("静音标记", "🔇" in text)
    check("tooltip 含按钮与输入框",
          "按钮：回复" in badge.toolTip() and "输入框：reply" in badge.toolTip()
          and "菜单：打开" in badge.toolTip(), badge.toolTip())

    triggered = []
    badge.actionTriggered.connect(lambda index: triggered.append(index))
    badge._on_link_activated("xht-action:1")
    badge._on_link_activated("http://example.com")
    badge._on_link_activated("xht-action:99")
    check("点击链接发出按钮下标", triggered == [1], triggered)

    # 真实鼠标事件的路由：点按钮 → actionTriggered；点空白 → clicked（标记已读）。
    # 用 QMouseEvent + sendEvent 直接投给目标控件：QTest.mouseClick 会走平台事件队列，
    # 同坐标的其它窗口可能一起收到，测试会变得时好时坏。
    from PySide6.QtCore import QEvent, QPoint, QPointF, Qt as QtCore
    from PySide6.QtGui import QMouseEvent

    def click(widget, point):
        position = QPointF(point)
        event = QMouseEvent(QEvent.Type.MouseButtonRelease, position, position,
                            QtCore.MouseButton.LeftButton, QtCore.MouseButton.LeftButton,
                            QtCore.KeyboardModifier.NoModifier)
        QApplication.sendEvent(widget, event)

    badge_clicks = NotificationBadge(None)
    badge_clicks._actions = (item.content.button_actions[0],)
    badge_clicks.setTextFormat(QtCore.TextFormat.RichText)
    badge_clicks.setText('<a href="%s0">回复</a>' % ACTION_SCHEME)
    badge_clicks.resize(80, 24)
    action_hits = []
    body_hits = []
    badge_clicks.actionTriggered.connect(lambda index: action_hits.append(index))
    badge_clicks.clicked.connect(lambda: body_hits.append(True))
    click(badge_clicks, QPoint(badge_clicks.width() // 2, badge_clicks.height() // 2))
    check("点按钮只派发动作，不当成点内容",
          action_hits == [0] and body_hits == [], (action_hits, body_hits))
    click(badge_clicks, QPoint(1, 1))
    check("点空白处只标记已读",
          body_hits == [True] and action_hits == [0], (body_hits, action_hits))

    # 本地图片：把样例 XML 的图片换成真实存在的文件
    local_xml = RICH_XML.replace("C:\\tmp\\hero.png", hero_path) \
                        .replace("file:///C:/tmp/logo.png",
                                 "file:///" + logo_path.replace("\\", "/"))
    local_content = parse_local(local_xml)
    badge.show_content(local_content, unread=1, app_name="微信", show_images=True)
    check("本地图片渲染出来", "<img" in badge.text() and "logo.png" in badge.text())
    badge.show_content(local_content, unread=1, app_name="微信", show_images=False)
    check("关掉图片后不渲染 <img>", "<img" not in badge.text())

    badge.show_content(item.content, unread=1, app_name="微信", show_actions=False)
    check("关掉按钮后没有链接", ACTION_SCHEME not in badge.text())
    check("关掉按钮后 badge 也不派发", badge._actions == ())

    badge.show_badge(2)
    check("只是图标时文本干净", badge.text() == "🔔2" and badge._actions == ())

    # ---- 真实窗口：AutoSetSize 要把展开后的内容装下（内容比尺寸估计更高时不能切掉）----
    from features.XHT.Lib import XHTWindow as XHT

    window = XHT.Window(
        config={"notify_enabled": False, "windowpos": "R", "edge_height": 4,
                "horizontal_edge_margin": 4, "drag_threshold": 8,
                "notify_mode": "expand"},
        elements=None, logger=NullLogger(),
    )
    window.notify_badge.show_content(item.content, unread=2, app_name="微信",
                                     show_images=True, show_actions=True)
    window.AutoSetSize()
    pump(app, 0.2)
    hint = window.global_layout.sizeHint()
    # 布局给的首选尺寸里，高度必须真的够放「这个宽度下换行后的内容」
    needed = window.notify_badge.heightForWidth(hint.width())
    check("窗口尺寸能装下展开的通知（含按钮行）",
          hint.height() >= needed, "布局=%s 该宽度下需要=%s" % (hint, needed))
    check("展开后的高度确实大于一行（不是被压扁的）", needed > 60, needed)
    window._shutdown_notifications()

    # ---- Presenter：完整点击链路（注入 dispatcher / prompt，不碰真实应用） ----
    dispatched = []

    def dispatcher(target_item, action=None, inputs=None, target="action"):
        dispatched.append((action.label if action is not None else None,
                           dict(inputs or {}), target))
        return FakeResult(ok=True, message="已发送「%s」" % (action.label if action else ""))

    # 通知部件在真机上就是小黑条的**子控件**，测试里也照这个层级来：
    # 免得它作为独立顶层窗口收到无关的鼠标事件（会让未读断言时好时坏）。
    window = FakeWindow()
    badge2 = NotificationBadge(window)
    presenter = NotificationPresenter(
        window, badge2,
        {"notify_enabled": True, "notify_mode": "expand", "notify_actions": True,
         "notify_images": True},
        None, dispatcher=dispatcher,
        prompt=lambda needed, action: {needed[0].id: "好的"},
    )
    check("构造时不起监听线程（便于测试）", not presenter.watcher.isRunning())

    presenter.on_notification(item)
    check("收到通知后未读为 1", presenter.unread == 1)
    check("展开模式把时间藏起来", window.time_visible is False)
    check("渲染出内容元素", "小明的消息" in badge2.text())
    check("记录了可点击按钮", len(presenter.last_actions) == 3)

    presenter.on_action(0)
    pump(app, 1.5)
    check("按钮点击 → 输入 → 发回应用",
          dispatched == [("回复", {"reply": "好的"}, "action")], dispatched)
    check("激活完成后清零未读", presenter.unread == 0)
    check("激活完成后给出反馈",
          presenter._feedback.startswith("✓") and "✓" in badge2.text(), presenter._feedback)

    # 取消输入：什么都不发
    dispatched.clear()
    presenter.on_notification(item)
    presenter.prompt = lambda needed, action: None
    presenter.on_action(0)
    pump(app, 0.5)
    check("取消输入时不激活", dispatched == [], dispatched)

    # 关掉按钮：不派发
    dispatched.clear()
    presenter.apply_config({"notify_enabled": True, "notify_mode": "expand",
                            "notify_actions": False}, start=False)
    presenter.prompt = lambda needed, action: {"reply": "x"}
    presenter.on_notification(item)
    check("关掉按钮后不渲染链接", ACTION_SCHEME not in badge2.text())
    presenter.on_action(0)
    pump(app, 0.5)
    check("关掉按钮后点击不派发", dispatched == [], dispatched)

    # 点击内容本体：默认不激活（保持「标记已读」的老行为）
    dispatched.clear()
    presenter.apply_config({"notify_enabled": True, "notify_mode": "expand",
                            "notify_actions": True, "notify_click_activates": False},
                           start=False)
    presenter.on_notification(item)
    presenter.on_clicked()
    pump(app, 0.5)
    check("默认点击内容只标记已读", dispatched == [] and presenter.unread == 0, dispatched)

    presenter.apply_config({"notify_enabled": True, "notify_mode": "expand",
                            "notify_click_activates": True}, start=False)
    presenter.on_notification(item)
    presenter.on_clicked()
    pump(app, 1.5)
    check("开启后点击内容会激活应用",
          dispatched == [(None, {}, "body")], dispatched)

    # 点 🔔（badge 模式）→ 打开系统通知中心，不激活应用，之后清零未读。
    # open_center 是注入点，测试里不碰真实系统。
    opened = []
    presenter.open_center = lambda: (opened.append(True), True)[1]
    presenter.apply_config({"notify_enabled": True, "notify_mode": "badge"}, start=False)
    dispatched.clear()
    presenter.on_notification(item)
    presenter.on_notification(item)
    check("badge 模式显示 🔔N", presenter.unread == 2 and "🔔" in badge2.text(),
          badge2.text())
    check("🔔 的 tooltip 提示会打开通知中心",
          badge2.toolTip() == "点击打开通知中心", badge2.toolTip())
    presenter.on_clicked()
    pump(app, 0.5)
    check("点击 🔔 打开系统通知中心", opened == [True], opened)
    check("点击 🔔 不激活应用", dispatched == [], dispatched)
    check("点击 🔔 后未读清零", presenter.unread == 0, presenter.unread)

    # 展开模式点内容时不应该去开通知中心
    opened.clear()
    presenter.apply_config({"notify_enabled": True, "notify_mode": "expand",
                            "notify_click_activates": False}, start=False)
    presenter.on_notification(item)
    presenter.on_clicked()
    pump(app, 0.5)
    check("展开模式点内容不打开通知中心", opened == [], opened)

    # 展开模式：内容自动收起后退化成 🔔N —— 显示的同样是 🔔，点它必须开通知中心
    opened.clear()
    dispatched.clear()
    presenter.apply_config({"notify_enabled": True, "notify_mode": "expand",
                            "notify_click_activates": True}, start=False)
    presenter.on_notification(item)
    presenter.on_notification(item)
    presenter._on_expire()          # 显示时间到：内容收起，未读还在 → 退化成 🔔N
    check("展开模式收起后退化成 🔔N",
          badge2.is_badge_only() and "🔔" in badge2.text(), badge2.text())
    presenter.on_clicked()
    pump(app, 0.5)
    check("点退化出来的 🔔N → 打开通知中心", opened == [True], opened)
    check("点退化出来的 🔔N → 不激活应用", dispatched == [], dispatched)
    check("点退化出来的 🔔N → 清零未读", presenter.unread == 0, presenter.unread)

    # 展开内容里那行小号 🔔N 是链接：点它同理，而不是「打开这条通知」
    opened.clear()
    dispatched.clear()
    presenter.on_notification(item)
    presenter.on_notification(item)
    check("展开内容里带可点的 🔔N 链接",
          CENTER_SCHEME in badge2.text() and "🔔2" in badge2.text(), badge2.text()[:120])
    badge2.linkActivated.emit(CENTER_SCHEME)
    pump(app, 0.5)
    check("点内容里的 🔔N 链接 → 打开通知中心", opened == [True], opened)
    check("点内容里的 🔔N 链接 → 不激活应用", dispatched == [], dispatched)
    check("点内容里的 🔔N 链接 → 清零未读", presenter.unread == 0, presenter.unread)

    # 退化形态之后又来新通知 → 重新展开内容，此时点内容不能再被当成「点 🔔」
    opened.clear()
    presenter.apply_config({"notify_enabled": True, "notify_mode": "expand",
                            "notify_click_activates": False}, start=False)
    presenter.on_notification(item)
    presenter._on_expire()              # 先退化成 🔔N
    presenter.on_notification(item)     # 新通知 → 又展开内容
    check("新通知到来后重新展开内容",
          not badge2.is_badge_only() and "小明的消息" in badge2.text(),
          badge2.text()[:60])
    presenter.on_clicked()
    pump(app, 0.5)
    check("重新展开后点内容不开通知中心", opened == [], opened)

    # 静默模式不动外观；未读仍累计
    presenter.apply_config({"notify_enabled": True, "notify_mode": "silent"}, start=False)
    badge2.clear()
    presenter.on_notification(item)
    check("静默模式不显示内容", not badge2.isVisible() or badge2.text() == "")
    check("静默模式仍累计未读", presenter.unread == 1)

    presenter.stop()
    check("stop() 幂等", True)

    # 激活期间又来了一条新通知：不要把它一起标成已读
    dispatched.clear()
    presenter.apply_config({"notify_enabled": True, "notify_mode": "expand",
                            "notify_actions": True}, start=False)
    presenter.prompt = lambda needed, action: {"reply": "x"}
    presenter.on_notification(item)
    presenter.on_action(0)
    newer = Notification(id=2, app_id="WeChat", app_name="微信", xml=RICH_XML,
                         texts=extract_texts(RICH_XML))
    presenter.on_notification(newer)
    unread_before = presenter.unread
    pump(app, 1.5)
    check("激活期间来的新通知不会被误清未读",
          presenter.unread == unread_before and unread_before > 0,
          "激活前=%d 激活后=%d" % (unread_before, presenter.unread))
    shutil.rmtree(workdir, ignore_errors=True)


def parse_local(xml):
    from features.catch_notify.toast import parse_toast
    return parse_toast(xml)


def test_watch_incremental():
    """增量监听：``[Order]`` 回退（它是 SQLite 的 rowid）之后仍必须收到新通知。"""
    print("\n=== 6) 增量监听的新通知识别 ===")
    import sqlite3
    import threading

    from features.catch_notify.database import NotificationDatabase

    workdir = scratch_dir("xht-watch-")
    path = os.path.join(workdir, "wpndatabase.db")
    _create_notification_db(path)

    database = NotificationDatabase(path)
    check("合成通知库可读（0 条）", database.count() == 0, database.count())

    stop = threading.Event()
    collected = []
    errors = []

    def reader():
        try:
            for item in database.watch(kinds=("toast",), interval=0.1,
                                       skip_existing=True, stop_event=stop):
                collected.append(item)
        except Exception as exc:  # noqa: BLE001 - 监听线程里的异常要报出来
            errors.append(exc)

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    time.sleep(0.3)

    # 一条老的 tile 占着 Order=1（对应真机上那条 2024 年的旧磁贴）
    _insert_notification(path, "旧磁贴", kind="tile")
    # 两条 toast：Order 依次 +1
    _insert_notification(path, "第一条")
    _insert_notification(path, "第二条")
    deadline = time.time() + 5
    while time.time() < deadline and len(collected) < 2:
        time.sleep(0.05)
    check("新 toast 按顺序收到", [item.title for item in collected] == ["第一条", "第二条"],
          [item.title for item in collected])
    if errors:
        check("监听线程无异常", False, repr(errors[0]))
        stop.set()
        thread.join(timeout=3)
        database.close()
        shutil.rmtree(workdir, ignore_errors=True)
        return

    # 关键场景：用户把**最新**那条点掉（或清空通知中心）之后，平台插入的新行
    # 拿到的是「当前表里最大 Order + 1」，也就是**回退**的 Order。
    _delete_newest_notification(path)
    _insert_notification(path, "第三条")
    deadline = time.time() + 5
    while time.time() < deadline and len(collected) < 3:
        time.sleep(0.05)
    check("最新一条被删掉后，新通知仍然收得到（Order 撞上旧游标）",
          [item.title for item in collected] == ["第一条", "第二条", "第三条"],
          [item.title for item in collected])

    # 更极端：通知中心被清空，Order 从最小值重新开始
    _delete_all_notifications(path)
    _insert_notification(path, "第四条")
    deadline = time.time() + 5
    while time.time() < deadline and len(collected) < 4:
        time.sleep(0.05)
    check("通知中心被清空后，新通知仍然收得到（Order 深回退）",
          [item.title for item in collected] == ["第一条", "第二条", "第三条", "第四条"],
          [item.title for item in collected])
    check("没有重复上报", len(collected) == len({id(item) for item in collected})
          and [item.title for item in collected] == ["第一条", "第二条", "第三条", "第四条"],
          [item.title for item in collected])

    stop.set()
    thread.join(timeout=3)
    database.close()
    shutil.rmtree(workdir, ignore_errors=True)


_SYNTHETIC_SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE NotificationHandler (
    RecordId INTEGER PRIMARY KEY, PrimaryId TEXT, HandlerType TEXT,
    CreatedTime TEXT, ModifiedTime TEXT);
CREATE TABLE HandlerAssets (
    HandlerId INTEGER, AssetKey TEXT, AssetValue TEXT,
    PRIMARY KEY (HandlerId, AssetKey));
CREATE TABLE Notification (
    [Order] INTEGER PRIMARY KEY, Id INTEGER NOT NULL, HandlerId INTEGER,
    ActivityId GUID, Type TEXT NOT NULL, Payload BLOB, Tag TEXT, [Group] TEXT,
    ExpiryTime INT64, ArrivalTime INT64, DataVersion INT64 DEFAULT 0,
    PayloadType TEXT NOT NULL, BootId INT64 DEFAULT 0,
    ExpiresOnReboot BOOLEAN DEFAULT FALSE);
CREATE TABLE Metadata (Key TEXT PRIMARY KEY, Value TEXT);
INSERT INTO Metadata (Key, Value) VALUES ('CurrentNotificationId', 0);
INSERT INTO NotificationHandler (RecordId, PrimaryId, HandlerType, CreatedTime, ModifiedTime)
    VALUES (1, 'XHT.Watch.Test', 'app:desktop', datetime('now'), datetime('now'));
INSERT INTO HandlerAssets (HandlerId, AssetKey, AssetValue)
    VALUES (1, 'DisplayName', '监听测试');
"""


def _create_notification_db(path: str) -> None:
    """造一个与真机同构的通知库（``[Order]`` 是 INTEGER PRIMARY KEY = rowid）。"""
    import sqlite3

    connection = sqlite3.connect(path)
    try:
        connection.executescript(_SYNTHETIC_SCHEMA)
        connection.commit()
    finally:
        connection.close()


def _insert_notification(path: str, title: str, kind: str = "toast",
                         order=None) -> int:
    """插入一条通知，尽量照着真机通知平台的写法来。

    * ``[Order]`` 不给（``order=None``）→ SQLite 按 rowid 分配 ``MAX(Order)+1``
      （这就是平台上「删掉较新的行之后 Order 会回退」的原因）；
    * ``Id`` 取 ``Metadata.CurrentNotificationId`` 并把它加一（平台自己就是这么做的，
      所以 Id 是全局单调、不复用的）；
    * ``ArrivalTime`` 用当前时间的 FILETIME。
    """
    import sqlite3

    xml = ('<toast><visual><binding template="ToastGeneric"><text>%s</text>'
           '<text>正文</text></binding></visual></toast>' % title)
    arrival = int((time.time() + 11644473600) * 10_000_000)
    connection = sqlite3.connect(path)
    try:
        cursor = connection.cursor()
        identifier = cursor.execute(
            "SELECT COALESCE(MAX(CAST(Value AS INTEGER)), 0) + 1 FROM Metadata "
            "WHERE Key = 'CurrentNotificationId'").fetchone()[0]
        cursor.execute(
            "INSERT OR REPLACE INTO Metadata (Key, Value) VALUES ('CurrentNotificationId', ?)",
            (str(identifier),))
        columns = "[Id], HandlerId, Type, Payload, PayloadType, Tag, [Group], ArrivalTime, ExpiryTime"
        values = [identifier, 1, kind, xml.encode("utf-8"), "Xml", "", "", arrival, 0]
        if order is not None:
            columns = "[Order], " + columns
            values = [order] + values
        cursor.execute(
            "INSERT INTO Notification (%s) VALUES (%s)"
            % (columns, ", ".join("?" * len(values))), values)
        connection.commit()
        return int(cursor.lastrowid)
    finally:
        connection.close()


def _delete_newest_notification(path: str) -> None:
    import sqlite3

    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "DELETE FROM Notification WHERE [Order] = "
            "(SELECT MAX([Order]) FROM Notification)")
        connection.commit()
    finally:
        connection.close()


def _delete_all_notifications(path: str) -> None:
    import sqlite3

    connection = sqlite3.connect(path)
    try:
        connection.execute("DELETE FROM Notification")
        connection.commit()
    finally:
        connection.close()


def test_pop_out_race():
    """新通知正好落在「隐藏动画播放中」那 250ms 里，也必须看得到。"""
    print("\n=== 7) 通知落在收起动画里也要能显示 ===")
    from features.XHT.Lib import XHTWindow as XHT
    from features.catch_notify.records import Notification, extract_texts

    class _Logger:
        def debug(self, *a, **k):
            pass

        info = warning = error = critical = debug

    window = XHT.Window(
        config={"notify_enabled": False, "windowpos": "R", "edge_height": 4,
                "horizontal_edge_margin": 4, "drag_threshold": 8,
                "notify_mode": "expand", "notify_duration": 6},
        elements=None, logger=_Logger(),
    )
    presenter = window.notify_presenter
    presenter.enabled = True
    presenter.mode = "expand"
    presenter.dispatcher = lambda *a, **k: FakeResult()
    presenter.prompt = lambda needed, action: None

    first = Notification(id=1, app_id="WeChat", app_name="微信", xml=RICH_XML,
                         texts=extract_texts(RICH_XML))
    second = Notification(id=2, app_id="WeChat", app_name="微信",
                          xml=RICH_XML.replace("小明的消息", "第二条通知"),
                          texts=extract_texts(RICH_XML))

    # 小黑条一开始是收起来的
    window.is_hidden = True
    presenter.on_notification(first)
    pump(app, 0.6)
    check("收起来的小黑条会被新通知弹出来", not window.is_hidden)

    # 显示时间到 → 开始收起动画
    presenter._on_expire()
    pump(app, 0.05)
    check("收起动画进行中", window.is_popping_hidden, window.is_hiding)

    # 就在这时候来了一条新通知
    presenter.on_notification(second)
    pump(app, 0.8)
    check("收起动画里的新通知不会被一起藏掉", not window.is_hidden,
          "is_hidden=%s is_hiding=%s" % (window.is_hidden, window.is_hiding))
    check("显示的是新那条", "第二条通知" in window.notify_badge.text(),
          window.notify_badge.text()[:80])

    presenter.stop()
    window._shutdown_notifications()


# --------------------------------------------------------------------------- #
def test_geometry_warnings():
    """尺寸变化不该刷 ``QWindowsWindow::setGeometry: Unable to set geometry``。

    这条警告是「请求的几何被布局/系统顶回去」时才有的：通知内容是换行富文本，宽度一变
    「该宽度需要的高度」就变，尺寸动画的中间帧如果按线性插值就会每帧被顶回 —— 表现为
    日志刷屏 + 窗口先被撑高再缩回。测试真的把窗口 show() 出来（挪到屏幕外），因为只有
    真实窗口才会走平台那套 setGeometry。
    """
    print("\n=== 8) 尺寸变化不刷 setGeometry 警告 ===")
    from PySide6.QtCore import qInstallMessageHandler

    from features.XHT.Lib import XHTWindow as XHT
    from features.catch_notify.records import Notification, extract_texts

    warnings = []

    def handler(mode, context, message):
        if "Unable to set geometry" in message:
            warnings.append(message)

    previous = qInstallMessageHandler(handler)
    try:
        window = XHT.Window(
            config={"notify_enabled": False, "windowpos": "M", "edge_height": 4,
                    "horizontal_edge_margin": 4, "drag_threshold": 8},
            elements=[], logger=NullLogger(),
        )
        window.move(0, -3000)       # 屏幕外面，别打扰用电脑的人
        window.show()
        pump(app, 0.3)

        item = Notification(id=1, app_id="WeChat", app_name="微信", xml=RICH_XML,
                            texts=extract_texts(RICH_XML))
        window.notify_badge.show_content(item.content, unread=2, app_name="微信")
        window.AutoSetSize()
        pump(app, 0.6)
        grown = window.size()

        # 收起（通知消失）
        window.notify_badge.clear()
        window.set_time_visible(True)
        window.AutoSetSize()
        pump(app, 0.6)
        shrunk = window.size()

        # 定时器每秒都会走这条路
        window.update_time()
        pump(app, 0.2)

        window.hide()
        window._shutdown_notifications()
    finally:
        qInstallMessageHandler(previous)

    check("尺寸变化不产生 setGeometry 警告", warnings == [],
          (len(warnings), warnings[0][:100] if warnings else ""))
    check("展开后确实变大了", grown.height() > shrunk.height() and grown.width() >= shrunk.width(),
          "%s → %s" % (grown, shrunk))


# --------------------------------------------------------------------------- #
def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass

    test_parsing()
    test_activation_plan()
    test_com_marshalling()
    test_registry_lookup()
    test_display()
    test_watch_incremental()
    test_pop_out_race()
    test_geometry_warnings()

    print()
    print("FAILED: %d" % len(failures))
    for name in failures:
        print("  - " + name)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
