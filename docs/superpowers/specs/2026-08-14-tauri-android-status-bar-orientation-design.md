# Tauri Android 状态栏与横屏沉浸式设计

**日期：** 2026-08-14  
**状态：** 用户已确认设计，待审阅本文档  
**范围：** 所有 Tauri Android 页面、共享移动端安全区样式、Android 初始化/打包流水线  
**不在范围：** iOS、浏览器 Web C 的系统栏策略；Android 导航栏不在本次隐藏范围内。

## 1. 目标

TermFlow 的 Android 客户端在 Android 15/targetSdk 36 的 edge-to-edge 窗口中，系统状态栏与 WebView 同时占据屏幕顶部。目标交互为：

1. **竖屏**：系统状态栏常显；应用顶栏背景延伸到状态栏下，标题和可点击控件避开状态栏、圆孔和刘海。
2. **横屏**：所有 Tauri Android 页面默认隐藏**系统状态栏**，保留导航栏；用户从系统栏边缘手势可临时拉出状态栏。
3. **横屏临时状态栏**：状态栏只覆盖 WebView，不改变页面高度、标题栏位置、终端画布位置或按键栏位置；自动消失后页面也不发生第二次位移。
4. **圆孔/刘海**：即使横屏隐藏状态栏，也继续避开物理 display cutout，尤其是旋转到左/右边的圆孔。

横屏不被禁用。终端在横屏的宽画布是保留该方向的重要原因。

## 2. 已确认的根因与约束

当前 tauri android init 生成的 MainActivity.kt 调用 enableEdgeToEdge()，生成工程的 targetSdk = 36；生成的 Manifest 已声明 orientation|screenSize 等 configChanges。Android 因而可以让 WebView 绘制到系统栏下。

前端两个页面族缺少顶部安全区：

- packages/client-ui/src/styles/app.css 的 .app-header 和 bare/login 页面没有为顶部系统栏预留内容空间；
- packages/client-ui/src/styles/terminal-responsive.css 的 .terminal-titlebar 仅预留左右安全区，.mobile-keybar-shell 仅预留底部安全区。

apps/clients/tauri/src-tauri/gen/android/ 是 tauri android init 产生的工程，不能将手工改动只留在该目录：CI 每次都会重新初始化。因此 Android 原生改动必须由仓库内、可测试、幂等且遇到模板漂移会失败的配置器在初始化后注入，和现有 Android 签名配置器采用同一策略。

Android WebView 会把系统栏/圆孔的安全区提供为 CSS safe-area-inset-*。但横屏临时显示状态栏时不能让顶部 env(safe-area-inset-top) 参与布局，否则标题与画布会发生用户明确不接受的重排。

## 3. 设计

### 3.1 Android 原生系统栏控制

新增 scripts/release/configure_android_system_bars.py，输入由 Tauri 生成的 MainActivity.kt。配置器只接受已知 Tauri Activity 模板和自身的唯一标记；缺少、重复或歧义标记时失败，绝不猜测性修改。

它保留现有 enableEdgeToEdge()，并注入 AndroidX WindowInsetsControllerCompat 逻辑：

    Activity 创建或方向变化
      ├─ 竖屏：show(statusBars)
      └─ 横屏：behavior = SHOW_TRANSIENT_BARS_BY_SWIPE
               hide(statusBars)

Activity 的 onConfigurationChanged 已会因生成 Manifest 的 configChanges 收到旋转事件；实现同时在 onCreate 调用一次，避免冷启动横屏与旋转横屏不一致。

只传递 WindowInsetsCompat.Type.statusBars() 给 show/hide。导航栏、手势条和键盘行为不变；用户可用系统手势临时拉出的状态栏是 transient overlay，而不是压缩 WebView 的普通布局栏。

### 3.2 前端安全区与稳定布局

Tauri 启动入口通过现有 @tauri-apps/plugin-os 的 platform()，只在 Android 设置根属性 data-tauri-platform="android"。共享 UI 不依赖 Tauri API，因此 Web/iOS 不会误用 Android 横屏沉浸式规则。

共享 CSS 定义一个顶部内容安全区变量：

    默认（所有移动/粗指针环境）：
      --termflow-top-content-inset = env(safe-area-inset-top)

    仅 Android + 横屏：
      --termflow-top-content-inset = 0px

竖屏使用这个变量为普通 .app-header、bare/login 的 main 和 .terminal-titlebar 增加顶部内容内边距；相应背景仍从屏幕最上方开始，因此状态栏与顶栏视觉融为一体，而不是出现一条空白带。普通页的 header/main 同时应用左右 safe-area-inset-left/right，终端现有左右与底部安全区规则继续保留。

Android 横屏不再让上述元素读取动态的顶部 env()：临时状态栏出现时，它会覆盖页面，但不会改变 grid 行高、100dvh 终端画布、按键栏或滚动位置。左右安全区仍使用 safe-area-inset-left/right，以避开横屏侧边的圆孔/刘海。

### 3.3 CI 和打包一致性

下列两个 Android 初始化点都在 tauri android init --ci 后、任何 Android build 前执行系统栏配置器：

- .github/workflows/ci.yml 的 tauri-android-unsigned；
- .github/workflows/tauri-packages.yml 的 android-apk（debug、signed candidate 与 tag release 共用）。

系统栏配置不读取秘密、不生成私钥、不改变签名顺序。发布流水线仍先初始化，再生成图标、注入签名（仅 release），最后构建 APK。

## 4. 测试与验收

### 自动化

1. 新增 Python 单测覆盖 Activity 配置器：
   - 已知 Tauri 模板注入所需 imports、方向回调、竖屏 show(statusBars)、横屏 transient-by-swipe + hide(statusBars)；
   - 第二次运行字节级不变；
   - 缺少或重复模板标记失败。
2. 更新移动响应式契约测试：
   - 竖屏 header、bare 页面和 terminal titlebar 使用顶部内容安全区；
   - Android 横屏规则将顶部内容安全区固定为 0px，不读取动态 safe-area-inset-top；
   - Android 横屏仍保留左右安全区，底部导航/按键栏继续保留 bottom inset；
   - Web/iOS 不命中 Android 专用横屏规则。
3. 更新打包工作流契约测试，断言两个 workflow 都在 android init 后、Android build 前运行系统栏配置器。
4. CI Android debug APK 与 signed candidate/release APK 编译 Kotlin，作为生成 Activity 的真实编译门禁。

### 真机验收

在顶部圆孔 Android 真机上验证竖屏与横屏、浅色/深色系统图标可读性：

- 竖屏：时间/信号/电量常显，顶栏文字和按钮没有被圆孔或状态栏遮挡；
- 横屏：状态栏默认隐藏，终端和普通 Tauri 页面均维持可用；
- 从系统栏边缘拉出状态栏：系统信息临时显示，但同一页面的标题栏、终端画布、移动按键栏和 scroll offset 在前后截图中坐标不变；
- 横屏侧边圆孔/刘海不覆盖返回、主题、退出、终端菜单或移动按键；
- 回到竖屏后状态栏立即恢复常显，页面安全区正确恢复。

## 5. 非目标与风险控制

- 不通过固定 24px/32px 等设备猜测处理状态栏；
- 不关闭 Android edge-to-edge，也不通过降低 targetSdk 规避 Android 15 行为；
- 不隐藏导航栏、键盘或系统手势；
- 不修改已生成 Android 工程后却遗漏 CI 初始化路径；
- 不以 CSS/单测通过替代顶部圆孔真机验收；
- iOS 和 Web C 继续使用各自的安全区行为，除共享的竖屏 CSS 安全区改进外不获得 Android 的横屏状态栏隐藏策略。
