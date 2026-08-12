# TermFlow v0.1.0-rc.5 Android Auth, Icons, and Release Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复 Android “其他设备授权”丢失私有域名的问题，验证本机浏览器 OAuth 路径，把 TermFlow `>_` 图标确定性写入 APK，并从 `v0.1.0-rc.5` 起建立可覆盖升级的固定签名和单调版本号基线。

**Architecture:** Tauri C 的两个授权入口共用一个“规范化 issuer → 持久化 → 拉取并校验 OAuth metadata”的准备函数；设备页只从持久化配置恢复服务器。Android 发布流水线先物化版本，再初始化移动工程、生成图标、注入临时 keystore、构建 release APK，最后用独立脚本核对包名、版本、证书和 launcher 资源。源码/单测、CI 产物、Android 真机、Windows 回归分别记录，不互相替代。

**Tech Stack:** Vue 3、TypeScript、Vitest、Tauri 2、Rust、Python 3.12、pytest、GitHub Actions、Android SDK `aapt`/`apksigner`。

---

## 实施边界和不变量

- rc.3/rc.4 的实际 APK 使用了不同的 Android Debug 证书，不能与 rc.5 建立原地升级链；真机首次迁移必须卸载旧包一次。
- rc.5 及其后续 Android 包必须使用同一份项目 keystore；keystore、密码和 `keystore.properties` 不进入 Git。
- Android 远程服务器仍强制 HTTPS/WSS；不能为绕过网络错误恢复明文 HTTP/WS。
- WebView 不接收 Admin Token；token、refresh token、device code、user code、PKCE verifier、DPoP 私钥和完整 callback URL不得写入日志。
- `metadata.issuer` 必须与 canonical issuer 完全相等；不接受路由 query 覆盖可信服务器地址。
- `v0.1.0-rc.5` 的发布 tag 只在所有自动门禁、APK 静态检查和用户要求的真机验收完成后创建；推送 tag 是外部发布动作，执行前再次获得用户确认。

## 变更文件总览

**新增：**

- `apps/clients/tauri/src/serverPreparation.ts`
- `apps/clients/tauri/src/serverPreparation.test.ts`
- `scripts/release/configure_android_signing.py`
- `scripts/release/verify_android_apk.py`
- `tests/release/test_configure_android_signing.py`
- `tests/release/test_android_apk_verifier.py`
- `docs/android-release.md`

**修改：**

- `apps/clients/tauri/src/views/NativeConnectView.vue`
- `apps/clients/tauri/src/views/NativeConnectView.test.ts`
- `apps/clients/tauri/src/views/NativeDeviceAuthorizeView.vue`
- `apps/clients/tauri/src/views/NativeDeviceAuthorizeView.test.ts`
- `apps/clients/tauri/src/nativeAuth.ts`
- `apps/clients/tauri/src/nativeAuth.test.ts`
- `apps/clients/tauri/src/diagnostics.ts`
- `apps/clients/tauri/src/diagnostics.test.ts`
- `scripts/release/build_version.py`
- `scripts/release/version_files.py`
- `apps/clients/tauri/src-tauri/tauri.android.conf.json`
- `tests/release/test_build_version.py`
- `tests/release/test_version_materialization.py`
- `.github/workflows/tauri-packages.yml`
- `.github/workflows/release.yml`
- `tests/release/test_packaging_workflow_contract.py`
- `tests/release/test_release_workflow_contract.py`
- `.gitignore`

## Task 1: 建立共享服务器准备函数

**Files:**

- Create: `apps/clients/tauri/src/serverPreparation.ts`
- Create: `apps/clients/tauri/src/serverPreparation.test.ts`

- [ ] **Step 1: 写失败测试，锁定调用顺序和 issuer 校验**

测试必须覆盖：输入带尾随 `/` 时规范化为 origin、先 `serverConfig.replace` 再调用 metadata loader、返回 canonical issuer 和 metadata、metadata issuer 不匹配时拒绝继续、日志只包含稳定事件与 issuer。根据现有安全契约，带额外 path/query/fragment 的服务器地址仍应被 `canonicalIssuer` 拒绝。

```ts
it('persists the canonical issuer before loading metadata', async () => {
  const calls: string[] = []
  replaceServer.mockImplementation(async (issuer: string) => { calls.push(`replace:${issuer}`) })
  const loadMetadata = vi.fn(async () => {
    calls.push('metadata')
    return metadataFor('https://termflow.example')
  })

  await expect(prepareNativeServer('https://termflow.example/', loadMetadata))
    .resolves.toMatchObject({ issuer: 'https://termflow.example' })
  expect(calls).toEqual(['replace:https://termflow.example', 'metadata'])
})

it('rejects metadata from another issuer', async () => {
  await expect(prepareNativeServer(
    'https://termflow.example',
    async () => metadataFor('https://attacker.example'),
  )).rejects.toThrow('issuer_mismatch')
})
```

- [ ] **Step 2: 运行单个测试，确认 RED**

Run:

```bash
npm run test:run --workspace @termflow/tauri-client -- src/serverPreparation.test.ts
```

Expected: FAIL，模块 `./serverPreparation` 尚不存在。

- [ ] **Step 3: 实现最小共享函数**

`serverPreparation.ts` 的公开契约固定为：

```ts
import type { OAuthMetadataResponse } from '@termflow/client-contracts'
import { logNativeEvent } from './diagnostics'
import { canonicalIssuer, serverConfig } from './serverConfig'

export type NativeServerPreparation = {
  issuer: string
  metadata: OAuthMetadataResponse
}

export async function prepareNativeServer(
  input: string,
  loadMetadata: () => Promise<OAuthMetadataResponse>,
): Promise<NativeServerPreparation> {
  const issuer = canonicalIssuer(input)
  void logNativeEvent({ event: 'metadata_started', issuer })
  try {
    await serverConfig.replace(issuer)
    const metadata = await loadMetadata()
    if (metadata.issuer !== issuer) throw new Error('issuer_mismatch')
    void logNativeEvent({ event: 'metadata_succeeded', issuer })
    return { issuer, metadata }
  } catch (error) {
    const errorCode = error instanceof Error && error.message === 'issuer_mismatch'
      ? 'issuer_mismatch'
      : 'metadata_failed'
    void logNativeEvent({ event: 'metadata_failed', issuer, level: 'error', errorCode })
    throw error
  }
}
```

不要把原始异常明文写进该层日志；HTTP transport 已负责输出脱敏后的底层错误。

- [ ] **Step 4: 运行测试，确认 GREEN**

Run:

```bash
npm run test:run --workspace @termflow/tauri-client -- src/serverPreparation.test.ts
```

Expected: PASS。

- [ ] **Step 5: 提交共享准备函数**

```bash
git add -- apps/clients/tauri/src/serverPreparation.ts apps/clients/tauri/src/serverPreparation.test.ts
git commit -m "fix(tauri): share native server preparation"
```

## Task 2: 修复“其他设备授权”跳过私有域名准备的入口

**Files:**

- Modify: `apps/clients/tauri/src/views/NativeConnectView.vue`
- Modify: `apps/clients/tauri/src/views/NativeConnectView.test.ts`

- [ ] **Step 1: 将现有入口测试改成失败回归测试**

在 hoisted mocks 中增加 `prepareNativeServer`，并让两个路径都调用它。设备授权回归测试必须输入与默认值不同的域名，证明不是碰巧使用了 `serverConfig.current`：

```ts
it('prepares the typed private issuer before entering device authorization', async () => {
  const { wrapper, router } = await render()
  await wrapper.get('#server-url').setValue('https://termflow.mcocdaa-newapi.xin/')
  await wrapper.get('[data-action="device-authorize"]').trigger('click')
  await flushPromises()

  expect(mocks.prepareNativeServer).toHaveBeenCalledWith(
    'https://termflow.mcocdaa-newapi.xin/',
    expect.any(Function),
  )
  expect(router.currentRoute.value.path).toBe('/connect/device')
  expect(mocks.authorizeNativeClient).not.toHaveBeenCalled()
})
```

再增加失败断言：`prepareNativeServer` 抛出 `ApiError('offline')` 时停留 `/connect`，显示网络提示，不打开浏览器、不跳设备页。

- [ ] **Step 2: 运行视图测试，确认 RED**

Run:

```bash
npm run test:run --workspace @termflow/tauri-client -- src/views/NativeConnectView.test.ts
```

Expected: FAIL，因为设备按钮仍直接 `router.push`。

- [ ] **Step 3: 让两个按钮共用准备函数**

把模板中的直接跳转：

```vue
@click="router.push({ path: '/connect/device', query: route.query })"
```

替换为：

```vue
@click="connectDevice"
```

脚本只保留 endpoint 规范化在浏览器路径中：

```ts
import { prepareNativeServer } from '../serverPreparation'
import { canonicalAuthorizeEndpoint, serverConfig } from '../serverConfig'

async function prepareServer() {
  return prepareNativeServer(issuer.value, () => runtime.api.oauth.metadata())
}

async function connectDevice() {
  busy.value = true
  message.value = ''
  try {
    await prepareServer()
    await router.push({ path: '/connect/device', query: route.query })
  } catch (error) {
    message.value = registrationErrorMessage(error)
  } finally {
    busy.value = false
  }
}

async function connect() {
  busy.value = true
  message.value = ''
  try {
    const prepared = await prepareServer()
    const authorizeEndpoint = canonicalAuthorizeEndpoint(
      prepared.issuer,
      prepared.metadata.authorization_endpoint,
    )
    await authorizeNativeClient(
      prepared.issuer,
      authorizeEndpoint,
      prepared.metadata.scopes_supported,
    )
    await verifyNativeConnection(runtime)
    toast.show({ text: '已连接', tone: 'success' })
    const target = typeof route.query.redirect === 'string'
      && route.query.redirect.startsWith('/')
      && !route.query.redirect.startsWith('//')
      ? route.query.redirect
      : '/'
    await router.replace(target)
  } catch (error) {
    message.value = registrationErrorMessage(error)
  } finally {
    busy.value = false
  }
}
```

`registrationErrorMessage` 同时把 `network_error` 映射到现有网络提示，并为 `issuer_mismatch` 增加“服务器身份与填写地址不一致，请检查反向代理配置”的提示。

- [ ] **Step 4: 运行视图测试与类型检查**

Run:

```bash
npm run test:run --workspace @termflow/tauri-client -- src/views/NativeConnectView.test.ts src/serverPreparation.test.ts
npm run typecheck --workspace @termflow/tauri-client
```

Expected: 全部 PASS；设备路径没有调用 `authorizeNativeClient`，浏览器路径仍验证凭据后导航。

- [ ] **Step 5: 提交入口修复**

```bash
git add -- apps/clients/tauri/src/views/NativeConnectView.vue apps/clients/tauri/src/views/NativeConnectView.test.ts
git commit -m "fix(tauri): preserve issuer for device authorization"
```

## Task 3: 设备页复用持久化 issuer，并补齐日志脱敏

**Files:**

- Modify: `apps/clients/tauri/src/views/NativeDeviceAuthorizeView.vue`
- Modify: `apps/clients/tauri/src/views/NativeDeviceAuthorizeView.test.ts`
- Modify: `apps/clients/tauri/src/diagnostics.ts`
- Modify: `apps/clients/tauri/src/diagnostics.test.ts`
- Modify: `apps/clients/tauri/src/nativeAuth.ts`
- Modify: `apps/clients/tauri/src/nativeAuth.test.ts`

- [ ] **Step 1: 写设备页恢复测试**

让测试中的 `serverConfig.current` 可变，并把它设为生产私有域名。断言页面 mounted 后：

```ts
expect(mocks.prepareNativeServer).toHaveBeenCalledWith(
  'https://termflow.mcocdaa-newapi.xin',
  expect.any(Function),
)
expect(mocks.begin).toHaveBeenCalledWith(expect.objectContaining({
  issuer: 'https://termflow.mcocdaa-newapi.xin',
}))
expect(wrapper.get('.native-device-server').text())
  .toContain('https://termflow.mcocdaa-newapi.xin')
```

增加 mismatch/offline 用例，断言不生成设备码，并显示对应可操作提示。

- [ ] **Step 2: 写凭据脱敏失败测试**

```ts
it('redacts device authorization codes', () => {
  const detail = sanitizeNativeDetail(new Error(
    'device_code=secret-device user_code=ABCD-EFGH code_verifier=verifier',
  ))
  expect(detail).not.toContain('secret-device')
  expect(detail).not.toContain('ABCD-EFGH')
  expect(detail).not.toContain('verifier')
})
```

- [ ] **Step 3: 运行两个测试，确认 RED**

Run:

```bash
npm run test:run --workspace @termflow/tauri-client -- src/views/NativeDeviceAuthorizeView.test.ts src/diagnostics.test.ts
```

Expected: FAIL；设备页仍自行重复准备，且 sanitizer 未显式覆盖 `device_code`/`user_code`。

- [ ] **Step 4: 设备页改用共享准备结果**

删除 `canonicalIssuer` 导入，改为：

```ts
import { prepareNativeServer } from '../serverPreparation'
import { serverConfig } from '../serverConfig'

const prepared = await prepareNativeServer(
  serverConfig.current,
  () => runtime.api.oauth.metadata(),
)
issuer.value = prepared.issuer
const result = await beginNativeDeviceAuthorization({
  issuer: prepared.issuer,
  scopes: prepared.metadata.scopes_supported,
  client: {
    name: 'TermFlow',
    platform: `${platform()} ${arch()}`,
    version: buildVersion,
  },
  create: (input) => runtime.api.oauth.createDeviceAuthorization(input),
  poll: async (input, signal) => {
    try {
      return await pollDeviceAuthorization({ issuer: prepared.issuer, ...input }, signal)
    } catch (error) {
      const code = error instanceof ApiError
        ? error.code
        : error instanceof Error
          ? error.message
          : typeof error === 'string' ? error : ''
      if (code === 'authorization_pending') status.value = '等待浏览器确认…'
      else if (code === 'slow_down') status.value = '服务器较忙，已放慢轮询…'
      throw error
    }
  },
})
```

设备页不读取 route query 中的服务器地址。`actionableMessage` 增加 `issuer_mismatch`，其他 pending/slow_down/deny/expire 行为保持不变。

- [ ] **Step 5: 扩展 sanitizer 的键名**

把敏感键正则扩展为：

```ts
(?:access_token|refresh_token|client_secret|code_verifier|device_code|user_code|dpop(?:_proof)?|authorization|token|secret)
```

保留 URL、callback、Bearer/DPoP 和 JWT 的既有脱敏规则。

- [ ] **Step 6: 锁定 Android 使用系统浏览器 deep link，而非桌面 loopback**

在 `nativeAuth.ts` 提取并实际使用：

```ts
export function usesLoopbackAuthorization(targetPlatform = platform()): boolean {
  return targetPlatform !== 'ios' && targetPlatform !== 'android'
}

browser: tauriAuthorizationBrowser({
  issuer: serverConfig.current,
  loopback: usesLoopbackAuthorization(),
}),
```

在 `nativeAuth.test.ts` 增加：

```ts
expect(usesLoopbackAuthorization('android')).toBe(false)
expect(usesLoopbackAuthorization('ios')).toBe(false)
expect(usesLoopbackAuthorization('windows')).toBe(true)
expect(usesLoopbackAuthorization('linux')).toBe(true)
```

与既有 `tauriAuthorization.test.ts` 的“先注册 deep-link listener，再调用系统 `openUrl`，只接受 `termflow://auth/callback`”用例一起构成静态回归证据。真机回跳仍在 Task 9 验收。

- [ ] **Step 7: 运行 Tauri 授权相关测试**

Run:

```bash
npm run test:run --workspace @termflow/tauri-client -- \
  src/serverPreparation.test.ts \
  src/views/NativeConnectView.test.ts \
  src/views/NativeDeviceAuthorizeView.test.ts \
  src/diagnostics.test.ts \
  src/nativeAuth.test.ts \
  src/adapters/tauriAuthorization.test.ts
```

Expected: 全部 PASS；已有 adapter 测试继续证明 Android deep link 使用 `termflow://auth/callback` 而非桌面 loopback listener。

- [ ] **Step 8: 提交设备页与脱敏修复**

```bash
git add -- \
  apps/clients/tauri/src/views/NativeDeviceAuthorizeView.vue \
  apps/clients/tauri/src/views/NativeDeviceAuthorizeView.test.ts \
  apps/clients/tauri/src/diagnostics.ts \
  apps/clients/tauri/src/diagnostics.test.ts \
  apps/clients/tauri/src/nativeAuth.ts \
  apps/clients/tauri/src/nativeAuth.test.ts
git commit -m "fix(tauri): restore device auth server safely"
```

## Task 4: 建立 prerelease 单调 Android versionCode

**Files:**

- Modify: `scripts/release/build_version.py`
- Modify: `scripts/release/version_files.py`
- Modify: `apps/clients/tauri/src-tauri/tauri.android.conf.json`
- Modify: `tests/release/test_build_version.py`
- Modify: `tests/release/test_version_materialization.py`

Android 版本码使用以下明确映射：

```text
versionCode = major * 1,000,000 + minor * 10,000 + patch * 100 + rank

dev.N   rank =  0 + N, N ∈ [0, 19]
alpha.N rank = 20 + N, N ∈ [0, 19]
beta.N  rank = 40 + N, N ∈ [0, 19]
rc.N    rank = 60 + N, N ∈ [0, 38]
stable  rank = 99

major ∈ [0, 2099], minor/patch ∈ [0, 99]，0.0.0 全系列无效。
```

因此：

```text
0.1.0-rc.4 -> 10064
0.1.0-rc.5 -> 10065
0.1.0      -> 10099
0.1.1-rc.1 -> 10161
```

- [ ] **Step 1: 写顺序和边界失败测试**

从 `build_version.py` 导入新的 `android_version_code`：

```py
def test_android_version_codes_preserve_release_order() -> None:
    versions = (
        "0.1.0-rc.4",
        "0.1.0-rc.5",
        "0.1.0",
        "0.1.1-rc.1",
    )
    assert [android_version_code(value) for value in versions] == [
        10_064,
        10_065,
        10_099,
        10_161,
    ]

@pytest.mark.parametrize("version", ["1.0.0-dev.20", "1.0.0-rc.39", "2100.0.0"])
def test_android_rank_overflow_is_rejected(version: str) -> None:
    with pytest.raises(ValueError, match="mobile bundle"):
        validate_version(version)
```

把 materialization 中 `1.4.0-rc.2` 的 expected versionCode 改为 `1_040_062`。

- [ ] **Step 2: 运行版本测试，确认 RED**

Run:

```bash
python -m pytest tests/release/test_build_version.py tests/release/test_version_materialization.py -q
```

Expected: 顺序测试 FAIL，当前所有 `0.1.0-rc.N` 都映射成相同值。

- [ ] **Step 3: 在一个地方实现并复用映射**

在 `build_version.py` 中新增公开函数：

```py
_ANDROID_STAGE_BASE = {"dev": 0, "alpha": 20, "beta": 40, "rc": 60}
_ANDROID_STAGE_MAX = {"dev": 19, "alpha": 19, "beta": 19, "rc": 38}


def android_version_code(version: str) -> int:
    release = version.split("+", 1)[0]
    core, separator, prerelease = release.partition("-")
    major, minor, patch = (int(component) for component in core.split("."))
    if (major, minor, patch) == (0, 0, 0) or major > 2099 or minor > 99 or patch > 99:
        raise ValueError("build version is outside the supported mobile bundle range")
    rank = 99
    if separator:
        stage, raw_number = prerelease.split(".", 1)
        number = int(raw_number)
        if number > _ANDROID_STAGE_MAX[stage]:
            raise ValueError("build version is outside the supported mobile bundle range")
        rank = _ANDROID_STAGE_BASE[stage] + number
    return major * 1_000_000 + minor * 10_000 + patch * 100 + rank
```

`validate_version` 在正则校验后调用该函数；`version_files.py` 删除重复的 `_android_version_code`，改为导入并调用 `android_version_code`。

同时把仓库当前开发基线 `tauri.android.conf.json` 的 `versionCode` 从 `1` 调整为 `100`，因为 `0.0.1-dev.0` 在新映射中是 `100`；否则 `prepare_version.py --check` 会在未打 tag 的仓库上失败。

- [ ] **Step 4: 运行版本/物化/检查测试**

Run:

```bash
python -m pytest \
  tests/release/test_build_version.py \
  tests/release/test_version_materialization.py \
  tests/release/test_check_version.py -q
```

Expected: 全部 PASS，物化 `0.1.0-rc.5` 时 `tauri.android.conf.json` 的 versionCode 为 `10065`。

- [ ] **Step 5: 提交版本策略**

```bash
git add -- \
  scripts/release/build_version.py \
  scripts/release/version_files.py \
  apps/clients/tauri/src-tauri/tauri.android.conf.json \
  tests/release/test_build_version.py \
  tests/release/test_version_materialization.py
git commit -m "fix(release): order Android prerelease versions"
```

## Task 5: 增加 APK 静态验收器

**Files:**

- Create: `scripts/release/verify_android_apk.py`
- Create: `tests/release/test_android_apk_verifier.py`

验收器必须同时验证：

- `package: name='io.termflow.client'`
- tag 物化后的 `versionName` 与 `versionCode`
- 只有一个 signer，证书 SHA-256 等于固定基线
- APK 中包含普通、圆形和 adaptive foreground launcher 资源
- APK launcher PNG 至少覆盖生成工程各密度的 TermFlow launcher 哈希
- APK 不包含 rc.3/rc.4 已知错误模板图标哈希

- [ ] **Step 1: 用最小 ZIP APK fixture 写失败测试**

测试不依赖 Android SDK；它直接创建 ZIP，覆盖资源枚举、哈希对比、模板哈希拒绝和工具输出解析：

```py
def test_rejects_known_template_launcher_hash(tmp_path: Path) -> None:
    apk = write_apk_zip(tmp_path, launcher_bytes=KNOWN_TEMPLATE_PNG)
    with pytest.raises(ValueError, match="template launcher"):
        verify_launcher_resources(apk, generated_res(tmp_path))

def test_parses_package_and_signer_contract() -> None:
    assert parse_badging(BADGING) == AndroidPackageMetadata(
        package_name="io.termflow.client",
        version_name="0.1.0-rc.5",
        version_code=10065,
    )
    assert parse_signers(APKSIGNER_OUTPUT) == ("A1B2C3",)
```

- [ ] **Step 2: 运行测试，确认 RED**

Run:

```bash
python -m pytest tests/release/test_android_apk_verifier.py -q
```

Expected: FAIL，验收器尚不存在。

- [ ] **Step 3: 实现纯函数和 CLI**

CLI 固定为：

```text
python scripts/release/verify_android_apk.py \
  --apk PATH \
  --generated-res apps/clients/tauri/src-tauri/gen/android/app/src/main/res \
  --expected-package io.termflow.client \
  --expected-version-name 0.1.0-rc.5 \
  --expected-version-code 10065 \
  --expected-cert-sha256 HEX
```

实现要点：

```py
KNOWN_TEMPLATE_LAUNCHER_SHA256 = frozenset({
    "75322a261ba38a23a25647af0d1298f204f3b3fafd317b8122a1b9a1f38284ff",
    "2425d59d27578f75ca97d31d9ae8385898badce3d6a1774bfc2f0fd191dc12c7",
    "320e552422179b81dae014ee6cc00561bd6e7455767b28f5518b8862a8c7987c",
    "7a9ae0632bfe5b28a1e6e9a7b38982fef62be07c95de46c26bd4f901ac6b9753",
    "44e5c3dc1dfb392f65e3dbcc9b986d30f10dd95b57e306657e56281b572fa684",
    "b1d19b8b78d0ed6903dd35b7640afba29b4cf02f3780e0d1cd46d9ebcbc93695",
    "0b250fc4451dfd1e5a41128234d93225726a2984448b0b966af25677b167d8de",
    "ab9397c9827aef4b3a1f1f917fc722d54abcf26488880c8bf9c724d1e59ab905",
    "dae1ff05b101efea50e4b622fe6a3af8ba8f761162fa7c4fd864adc7cb39eeac",
    "27cf0cdbc78bec8b9a14eaedb084c541a3c191fe5db89766e831fbfd21ce955d",
})

LAUNCHER_NAMES = ("ic_launcher.png", "ic_launcher_round.png", "ic_launcher_foreground.png")
DENSITIES = ("mdpi", "hdpi", "xhdpi", "xxhdpi", "xxxhdpi")
```

`verify_launcher_resources` 必须对 `DENSITIES × LAUNCHER_NAMES` 建立生成资源与 APK `res/mipmap-<density>-v4/` 的一一映射，并确认 `mipmap-anydpi-v26/ic_launcher.xml` 的 adaptive foreground/background 引用存在。上述完整 SHA-256 来自已检查的 rc.4 APK 五个 density 的 launcher/foreground；round launcher 与同密度普通 launcher 相同。PNG 原始字节相同则直接比较 SHA-256；若 Android resource compiler 重压缩 PNG，则用 Python 标准库解码非交错 PNG 的 scanline/filter 后比较规范化像素摘要，不能因压缩差异跳过内容匹配。

`read_badging` 固定执行 `aapt dump badging APK` 并用严格正则提取唯一 `package` 行的 name/versionCode/versionName；`read_signers` 固定执行 `apksigner verify --print-certs APK` 并只接收 SHA-256 digest 行。证书比较先移除冒号并转大写，`len(signers) != 1` 直接失败。两个工具先从 `PATH` 查找，找不到时从 `ANDROID_SDK_ROOT` 或 `ANDROID_HOME` 下最高版本的 `build-tools` 解析；仍找不到就明确失败。

- [ ] **Step 4: 运行验收器单测**

Run:

```bash
python -m pytest tests/release/test_android_apk_verifier.py -q
```

Expected: 全部 PASS。

- [ ] **Step 5: 提交验收器**

```bash
git add -- scripts/release/verify_android_apk.py tests/release/test_android_apk_verifier.py
git commit -m "test(android): verify release APK identity"
```

## Task 6: 让 Android 发布包生成 TermFlow 图标并使用固定签名

**Files:**

- Create: `scripts/release/configure_android_signing.py`
- Create: `tests/release/test_configure_android_signing.py`
- Modify: `.github/workflows/tauri-packages.yml`
- Modify: `.github/workflows/release.yml`
- Modify: `tests/release/test_packaging_workflow_contract.py`
- Modify: `tests/release/test_release_workflow_contract.py`
- Modify: `.gitignore`

GitHub repository 预先配置以下五个 Actions secrets：

```text
ANDROID_KEYSTORE_BASE64
ANDROID_KEYSTORE_PASSWORD
ANDROID_KEY_ALIAS
ANDROID_KEY_PASSWORD
ANDROID_SIGNING_CERT_SHA256
```

`ANDROID_SIGNING_CERT_SHA256` 是固定 keystore 中唯一 signer 证书的 SHA-256；不是 rc.3/rc.4 debug 证书指纹。

- [ ] **Step 1: 先写 workflow 契约失败测试**

测试必须要求：

- reusable workflow 声明五个 secrets；
- manual/reusable workflow 声明默认关闭的 `signed_android_candidate`；
- release caller 使用 `secrets: inherit`；
- Android job 名称不再带 `debug`；
- `android init` 之后、build 之前执行 `tauri icon app-icon.svg`；
- release build 前调用受测试脚本给生成的 `app/build.gradle.kts` 注入 release signing config；
- tag release 缺任一签名 secret 时明确失败；
- release 分支不带 `--debug`，manual 分支仍可构建 debug APK；
- release APK 上传前调用 `verify_android_apk.py`；
- artifact suffix 改为 `android-arm64-apk`；
- release notes 不再声明 “Android uses a debug key”。

```py
def test_android_release_is_signed_verified_and_iconized() -> None:
    workflow = _workflow(CLIENT_WORKFLOW)
    job = workflow["jobs"]["android-apk"]
    steps = job["steps"]
    init = step_index(steps, "android init --ci")
    icon = step_index(steps, "icon app-icon.svg")
    signing = step_index(steps, "configure_android_signing.py")
    release_build = step_index(steps, "android build --ci --target aarch64 --apk")
    verify = step_index(steps, "verify_android_apk.py")
    upload = step_index_by_action(steps, "actions/upload-artifact@")
    assert init < icon < signing < release_build < verify < upload
```

- [ ] **Step 2: 运行 workflow 测试，确认 RED**

Run:

```bash
python -m pytest \
  tests/release/test_configure_android_signing.py \
  tests/release/test_packaging_workflow_contract.py \
  tests/release/test_release_workflow_contract.py -q
```

Expected: FAIL；当前 job 是 `android-debug-apk`，图标未同步，release 仍用 debug key。

- [ ] **Step 3: 用 TDD 实现生成 Gradle 的 signing 注入器**

Tauri 2.11.4 的 `android init` 模板默认没有 release `signingConfigs`，只写 `keystore.properties` 不会签名。因此新增一个小型、幂等、遇到模板漂移就失败的脚本。

先为 `configure_gradle(source: str) -> str` 写测试：

```py
def test_configures_release_signing_once() -> None:
    updated = configure_gradle(TAURI_2_11_4_GRADLE_FIXTURE)
    assert 'import java.io.FileInputStream' in updated
    assert 'create("release")' in updated
    assert 'rootProject.file("keystore.properties")' in updated
    assert 'signingConfig = signingConfigs.getByName("release")' in updated
    assert configure_gradle(updated) == updated

def test_rejects_unknown_template() -> None:
    with pytest.raises(ValueError, match="unsupported Tauri Android Gradle template"):
        configure_gradle("plugins {}")
```

实现只允许每个 marker 出现一次，在 `import java.util.Properties` 后插入 `FileInputStream`，在 `buildTypes` 前插入：

```kotlin
signingConfigs {
    create("release") {
        val keystorePropertiesFile = rootProject.file("keystore.properties")
        val keystoreProperties = Properties().apply {
            FileInputStream(keystorePropertiesFile).use { load(it) }
        }
        keyAlias = keystoreProperties.getProperty("keyAlias")
        keyPassword = keystoreProperties.getProperty("keyPassword")
        storeFile = rootProject.file(keystoreProperties.getProperty("storeFile"))
        storePassword = keystoreProperties.getProperty("storePassword")
    }
}
```

并在既有 `getByName("release")` 的第一行插入：

```kotlin
signingConfig = signingConfigs.getByName("release")
```

CLI 固定为：

```bash
python scripts/release/configure_android_signing.py \
  --gradle apps/clients/tauri/src-tauri/gen/android/app/build.gradle.kts \
  --properties apps/clients/tauri/src-tauri/gen/android/keystore.properties
```

同一测试文件还要验证 `write_keystore_properties(path, environment)`：缺任一环境变量失败；含反斜杠、`=`、`:` 或前导空格的值经过 Java Properties 转义；输出文件结尾有换行且只包含四个所需字段。

Run:

```bash
python -m pytest tests/release/test_configure_android_signing.py -q
```

Expected: PASS；同一文件运行两次不产生额外 diff，模板 marker 变化时明确失败而不是生成未签名包。

- [ ] **Step 4: 声明 reusable workflow secrets**

在 `.github/workflows/tauri-packages.yml` 的 `workflow_call` 下增加五个 `required: false` secret。之所以不是 schema 级 `required: true`，是为了让手动非发布 debug 构建继续工作；release job 内必须逐个强制检查。

同时给 `workflow_dispatch` 和 `workflow_call` 增加默认 `false` 的布尔输入 `signed_android_candidate`。它只控制 Android job 是否走固定证书 release build，不创建 tag、不发布 Release；tag 调用仍由 `is_release` 自动进入相同路径。`validate-version` 输出 `android_release_build=true` 的条件是“有效 release tag 或 signed candidate”。这使 rc.5 在发 tag 前就能验证真实签名产物，而不需要临时修改 workflow。

- [ ] **Step 5: 调整 Android job 的确定性顺序**

把 job 改名为 `android-apk` / `Android arm64 · APK`，步骤顺序固定为：

```yaml
- name: Generate the Android project
  run: npm run tauri --workspace @termflow/tauri-client -- android init --ci

- name: Generate TermFlow launcher resources
  run: npm run tauri --workspace @termflow/tauri-client -- icon app-icon.svg

- name: Configure Android release signing
  if: ${{ needs.validate-version.outputs.android_release_build == 'true' }}
  env:
    ANDROID_KEYSTORE_BASE64: ${{ secrets.ANDROID_KEYSTORE_BASE64 }}
    ANDROID_KEYSTORE_PASSWORD: ${{ secrets.ANDROID_KEYSTORE_PASSWORD }}
    ANDROID_KEY_ALIAS: ${{ secrets.ANDROID_KEY_ALIAS }}
    ANDROID_KEY_PASSWORD: ${{ secrets.ANDROID_KEY_PASSWORD }}
    ANDROID_SIGNING_CERT_SHA256: ${{ secrets.ANDROID_SIGNING_CERT_SHA256 }}
  shell: bash
  run: |
    set -euo pipefail
    for name in ANDROID_KEYSTORE_BASE64 ANDROID_KEYSTORE_PASSWORD ANDROID_KEY_ALIAS ANDROID_KEY_PASSWORD ANDROID_SIGNING_CERT_SHA256; do
      if [[ -z "${!name}" ]]; then
        echo "Missing required Android release secret: $name" >&2
        exit 1
      fi
    done
    keystore="$RUNNER_TEMP/termflow-android-release.jks"
    printf '%s' "$ANDROID_KEYSTORE_BASE64" | base64 --decode > "$keystore"
    export ANDROID_KEYSTORE_PATH="$keystore"
    python scripts/release/configure_android_signing.py \
      --gradle apps/clients/tauri/src-tauri/gen/android/app/build.gradle.kts \
      --properties apps/clients/tauri/src-tauri/gen/android/keystore.properties

- name: Build the signed release APK
  if: ${{ needs.validate-version.outputs.android_release_build == 'true' }}
  run: npm run tauri --workspace @termflow/tauri-client -- android build --ci --target aarch64 --apk

- name: Build a manual debug APK
  if: ${{ needs.validate-version.outputs.android_release_build != 'true' }}
  run: npm run tauri --workspace @termflow/tauri-client -- android build --debug --ci --target aarch64 --apk
```

`configure_android_signing.py` 从环境读取 `ANDROID_KEYSTORE_PATH`、密码和 alias，用 Java Properties 规则转义反斜杠、换行、`=`、`:` 和前导空格后写 properties；测试必须覆盖含特殊字符的密码，避免 shell `printf` 生成不可解析的签名配置。

图标命令必须在 `android init` 后运行，使 `gen/android/app/src/main/res` 的普通/round/adaptive launcher 都由 `apps/clients/tauri/app-icon.svg` 生成。不得只修改桌面的 `bundle.icon`。

- [ ] **Step 6: 构建后选择唯一 APK 并验收**

使用一个 “Resolve Android APK” 步骤把唯一文件写入 `GITHUB_OUTPUT`；release/signed candidate 只接受 `*-release.apk`，manual 只接受 `*-debug.apk`。验收步骤显式设置 `TERMFLOW_BUILD_VERSION`、计算 `ANDROID_VERSION_CODE`，并从 secret 注入证书指纹。不要把可选参数拼成未重新解析的 shell 字符串；使用数组：

```bash
set -euo pipefail
ANDROID_VERSION_CODE="$(python -c \
  'import sys; from scripts.release.build_version import android_version_code; print(android_version_code(sys.argv[1]))' \
  "$TERMFLOW_BUILD_VERSION")"
verify_args=(
  --apk "$APK_PATH"
  --generated-res apps/clients/tauri/src-tauri/gen/android/app/src/main/res
  --expected-package io.termflow.client
  --expected-version-name "$TERMFLOW_BUILD_VERSION"
  --expected-version-code "$ANDROID_VERSION_CODE"
)
if [[ "$ANDROID_RELEASE_BUILD" == "true" ]]; then
  verify_args+=(--expected-cert-sha256 "$ANDROID_SIGNING_CERT_SHA256")
fi
python scripts/release/verify_android_apk.py "${verify_args[@]}"
```

`ANDROID_VERSION_CODE` 用 `scripts.release.build_version.android_version_code` 从已解析版本生成。tag release 和 signed candidate 必须传期望证书；manual debug 只验包名、版本和图标。上传前将 APK 复制成固定文件名：

```text
TermFlow-<version>-android-arm64.apk
```

artifact 名改成 `${artifact_prefix}-android-arm64-apk`。

上传完成后增加 `if: ${{ always() }}` 的 cleanup 步骤，只删除 `$RUNNER_TEMP/termflow-android-release.jks` 和生成工程中的 `keystore.properties`；不使用宽泛目录或 glob。

- [ ] **Step 7: 传递 secrets 并修正 release notes**

`.github/workflows/release.yml`：

```yaml
package-clients:
  name: Package native C
  needs: validate-version
  uses: ./.github/workflows/tauri-packages.yml
  secrets: inherit
  with:
    platform: all
    release_tag: ${{ github.ref_name }}
```

发布说明改为：Windows 仍未签名；Android release APK 使用 TermFlow 固定证书；iOS 仍仅 Simulator。说明 rc.3/rc.4 Android 用户安装 rc.5 前需卸载一次。

- [ ] **Step 8: 阻止本地签名材料入库**

`.gitignore` 增加：

```gitignore
*.jks
*.keystore
keystore.properties
```

- [ ] **Step 9: 运行 release 契约测试**

Run:

```bash
python -m pytest \
  tests/release/test_configure_android_signing.py \
  tests/release/test_android_apk_verifier.py \
  tests/release/test_packaging_workflow_contract.py \
  tests/release/test_release_workflow_contract.py \
  tests/release/test_build_version.py \
  tests/release/test_version_materialization.py -q
```

Expected: 全部 PASS。

- [ ] **Step 10: 提交 Android 构建链**

```bash
git add -- \
  scripts/release/configure_android_signing.py \
  tests/release/test_configure_android_signing.py \
  .github/workflows/tauri-packages.yml \
  .github/workflows/release.yml \
  tests/release/test_packaging_workflow_contract.py \
  tests/release/test_release_workflow_contract.py \
  .gitignore
git commit -m "build(android): sign and verify release APKs"
```

## Task 7: 写 rc.5 Android 发布和迁移 runbook

**Files:**

- Create: `docs/android-release.md`

- [ ] **Step 1: 写可执行 runbook**

文档包含以下明确步骤：

1. 在受控离线位置生成/备份项目 keystore，记录 alias 和证书 SHA-256；至少保存两份加密备份。
2. 把 base64 keystore、密码、alias、证书指纹配置到五个 GitHub Actions secrets。
3. 先从非 tag 分支手动运行 Android packaging：普通 manual 验证 debug 路径；`signed_android_candidate=true` 验证固定证书 release 路径，两者均不发布。
4. tag release 产物必须通过 workflow 内的 package/version/cert/icon 检查。
5. rc.3/rc.4 用户：导出需要保留的信息，卸载旧包，安装 rc.5，重新登录一次。
6. rc.5 后升级验证：用同一 keystore 构建 `0.1.0-rc.6` 候选，执行 `adb install -r TermFlow-0.1.0-rc.6-android-arm64.apk`，确认无需卸载且应用数据/凭据仍存在。
7. 证书丢失时不能发布“可覆盖更新”的包；必须按新 application ID/迁移方案处理，不能生成另一份同名 debug key。

文档同时给出验收证据表：commit/tag、Actions run URL、APK SHA-256、package、versionName/versionCode、cert SHA-256、真机型号/Android 版本、两条授权结果、重启恢复结果、升级结果、Windows 回归结果。

- [ ] **Step 2: 检查文档中没有秘密或错误承诺**

Run:

```bash
rg -n "ANDROID_KEYSTORE|password|debug key|真机|Windows|adb install -r" docs/android-release.md
git diff --check
```

Expected: 只出现 secret 名称，不出现真实值；明确区分自动门禁与真机/Windows 验收。

- [ ] **Step 3: 提交 runbook**

```bash
git add -- docs/android-release.md
git commit -m "docs(android): add rc5 release runbook"
```

## Task 8: 执行本地完整回归，但不冒充 Android 真机结果

**Files:**

- Verify only; no source changes expected.

- [ ] **Step 1: 运行 Tauri client 全量单测和类型检查**

Run:

```bash
npm run test:run --workspace @termflow/tauri-client
npm run typecheck --workspace @termflow/tauri-client
```

Expected: 全部 PASS。

- [ ] **Step 2: 运行 B/Web C 授权回归**

Run:

```bash
python -m pytest \
  apps/control-plane/tests/test_oauth_api.py \
  apps/control-plane/tests/test_oauth_device_flow.py \
  tests/e2e/test_device_authorization.py \
  tests/e2e/test_unified_auth.py -q
```

这些用例覆盖 metadata、device code create/approve/pending/slow_down/deny/expire/token exchange、浏览器 session/TOTP 和真实进程的跨设备审批。

Expected: 全部 PASS。若某类仅由单元测试覆盖，结果必须标注“静态/进程内”，不能写成生产域名验收。

- [ ] **Step 3: 运行 release Python 测试**

Run:

```bash
python -m pytest tests/release -q
```

Expected: 全部 PASS。

- [ ] **Step 4: 运行仓库 Tauri 验证脚本**

Run:

```bash
scripts/verify-tauri.sh
```

Expected: PASS；如本机缺少 Android SDK、Rust target 或 GUI 运行条件，精确记录缺失门禁，等待 CI/真机补齐。

- [ ] **Step 5: 检查工作树和提交边界**

Run:

```bash
git diff --check
git status --short
git log --oneline --decorate -8
```

Expected: 无未预期文件、无 secret/keystore、每项提交只包含本计划范围。

## Task 9: CI 构建 rc.5 候选产物并做 Android/Windows 验收

**Files:**

- External CI and devices; no automatic source changes.

- [ ] **Step 1: 在创建 tag 前验证 secrets 和 signed candidate workflow**

先配置五个 secrets，再对当前 commit 运行 `Package C · Native Clients`：platform 选择 `android`，version 使用 `0.1.0-rc.5`，`signed_android_candidate=true`。这条路径构建与 tag release 相同的 release-signing APK，但不创建 GitHub Release。下载产物并记录 Actions run URL 和 APK SHA-256。

候选 APK 必须由 workflow 内验收器确认：

```text
package             io.termflow.client
versionName         0.1.0-rc.5
versionCode         10065
signer SHA-256      ANDROID_SIGNING_CERT_SHA256
launcher            TermFlow >_，无已知模板哈希
```

Expected: package/version/icon/cert 检查全部通过。另跑一次默认 `signed_android_candidate=false` 的 manual debug 构建，只用来证明开发路径未受影响；不得把 debug APK 当作 rc.5 正式 APK。

- [ ] **Step 2: Android 真机首次迁移验收**

1. 记录现有 rc.4 无法直接覆盖 rc.5 是预期的证书迁移边界。
2. 卸载 rc.4；该操作会删除旧应用本地数据，执行者须确认。
3. 安装 rc.5 release APK。
4. 检查 Android 桌面/应用列表图标为 TermFlow `>_`，不是黄色/蓝色斜“8”。
5. 输入 `https://termflow.mcocdaa-newapi.xin/`，点击“其他设备授权”，确认设备页显示 canonical 域名，另一设备审批后进入工作区。
6. 清除登录状态或重新安装候选，再走“本机浏览器登录”，确认系统浏览器打开、Web C 登录/审批、`termflow://` 返回 App、受保护 API 成功。
7. 强制停止并重启 App，确认凭据恢复和工作区可访问。

每一步记录实际结果、Android 版本、设备型号和失败日志；设备码/token 不进入记录。

- [ ] **Step 3: Android 覆盖升级验收**

在测试设备或可恢复快照上，用同一 keystore 构建 `0.1.0-rc.6`（versionCode `10066`）的 signed candidate；不要发布该版本。执行：

```bash
adb install -r TermFlow-0.1.0-rc.6-android-arm64.apk
```

Expected: 安装成功且不卸载 rc.5，App 数据和已登录状态保留。验收包不进入 GitHub Release；该设备此后高于 rc.5，如需最终 rc.5 再验须使用另一设备或先卸载。

- [ ] **Step 4: Windows 回归**

在 Windows 构建/安装同一 commit 的 C：

1. 登录页默认/已保存地址行为保持原样；
2. 私有域名本机浏览器 OAuth 成功；
3. `termflow://` 或现有 Windows loopback callback 正常完成；
4. WSS 终端不再出现此前的持续重连；
5. 桌面图标保持现有好看的 TermFlow 图标。

Windows 成功不能替代 Android 结果，Android 成功也不能替代 Windows 回归。

## Task 10: 发布 `v0.1.0-rc.5`

**Files:**

- Git tag/GitHub Actions/GitHub Release; external state change.

- [ ] **Step 1: 发布前停点**

汇总以下证据给用户并请求最终发布确认：

- 本地 Tauri/release/B-Web C 测试结果；
- 当前 commit SHA 和 clean worktree；
- Android 候选 APK 的包名、`0.1.0-rc.5`、`10065`、固定证书和图标检查；
- Android 两种授权、重启恢复、覆盖升级真机结果；
- Windows OAuth/WSS 回归结果；
- 已知非目标：Windows unsigned、iOS Simulator-only。

- [ ] **Step 2: 获得用户明确确认后创建并推送 tag**

先确认实施提交已经集成到 `main`、远端目标正确且当前 worktree clean，再执行：

```bash
git push origin main
git tag v0.1.0-rc.5
git push origin v0.1.0-rc.5
```

不要重新推送/移动已存在 tag；如果 tag 已存在，停止并报告远端状态。

- [ ] **Step 3: 等待完整 release matrix**

必须等待 `validate-version`、A、全部 native C（Windows/Linux/macOS/Android/iOS）、B+Web C、publish 和 provenance/镜像验证完成。Android job 需显示 release-signing 和 APK verifier 成功。

- [ ] **Step 4: 下载最终 Release APK 再验一次**

对 GitHub Release 下载的最终 APK 运行 `verify_android_apk.py`，并核对其 SHA-256 与 Actions artifact/Release `SHA256SUMS`。最终报告分别列出：

- 已完成：源码、测试、CI、最终 artifact 静态检查；
- 已完成或待完成：最终 Release APK 的真机安装/登录；
- 明确限制：未做的商店发布、Windows 代码签名和 iOS 真机分发。

只有这些证据都齐全，才能称 rc.5 发布完成。
