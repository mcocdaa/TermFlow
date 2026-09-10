# Android Release Signing Bootstrap Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create TermFlow's first Android release signing identity, prove that its encrypted recovery package is usable, configure the five GitHub Actions secrets, and validate one signed Android candidate without publishing a release.

**Architecture:** This is an operational bootstrap, not a repository feature. Generate one RSA-4096 JKS inside a permission-restricted temporary directory, encrypt the only durable recovery package to the existing `id_github.com.pub` SSH recipient with `age`, prove recovery before uploading secrets, and then dispatch the existing Android-only candidate workflow. Keep every secret out of command arguments, logs, chat, Git, and shell tracing.

**Tech Stack:** Bash, OpenJDK `keytool`, OpenSSL, `age`, GitHub CLI, Android SDK `apksigner`, existing `scripts/release/verify_android_apk.py`

---

## Safety invariants

- Never enable `set -x` and never print, copy into chat, or pass as command-line arguments any password, keystore bytes, or decrypted recovery manifest.
- Do not generate signing material until `gh auth status` confirms an authenticated account with write access to `mcocdaa/TermFlow`.
- The permanent local output is exactly `/home/mcocdaa/TermFlow-release-secrets/termflow-android-release-2026-08-13.tar.age`, mode `0600` inside a mode `0700` directory.
- The package is not considered backed up until it has been decrypted with `/home/mcocdaa/.ssh/id_github.com` and its JKS and certificate fingerprint have been revalidated.
- Upload exactly five Actions repository secrets. Do not create environment secrets or Actions variables.
- Do not move, recreate, or overwrite `v0.1.0-rc.5`; the validation run is a non-publishing `workflow_dispatch` candidate.
- Do not claim independent backup redundancy: after completion, the user still needs to copy the encrypted `.age` file to a second device/location.

### Task 1: Restore GitHub CLI authentication and verify repository authority

**Files:**
- Read: `/home/mcocdaa/.config/gh/hosts.yml` through `gh` only; do not print token contents
- Modify: none in the repository

- [ ] **Step 1: Confirm the current authentication failure without exposing credentials**

Run:

```bash
gh auth status
```

Expected: either a valid authenticated `mcocdaa` account or the already-observed invalid-token error. Do not inspect or print `hosts.yml` directly.

- [ ] **Step 2: Re-authenticate only if Step 1 is invalid**

Run in an interactive terminal:

```bash
gh auth login --hostname github.com --git-protocol ssh --web
```

Expected: the browser/device flow completes and `gh` stores a new credential. If the device endpoint times out, stop here; do not generate signing material while upload authority is unavailable.

- [ ] **Step 3: Verify identity, scopes, and repository visibility**

Run:

```bash
gh auth status
gh repo view mcocdaa/TermFlow --json nameWithOwner,viewerPermission,isPrivate
```

Expected: `nameWithOwner` is `mcocdaa/TermFlow` and `viewerPermission` is `ADMIN` or `MAINTAIN`. Record only those non-secret fields.

### Task 2: Install and validate the recovery encryption tool

**Files:**
- Read: `/home/mcocdaa/.ssh/id_github.com.pub`
- Modify: none in the repository

- [ ] **Step 1: Confirm the selected SSH recipient and its private key exist**

Run:

```bash
test -f /home/mcocdaa/.ssh/id_github.com.pub
test -f /home/mcocdaa/.ssh/id_github.com
ssh-keygen -lf /home/mcocdaa/.ssh/id_github.com.pub
```

Expected: an ED25519 key with fingerprint `SHA256:Ct/eCz69lu4sPwIh4DHTSFisVd8dV8vuxw5kfUbjsqg`. A mismatch is a hard stop requiring design review.

- [ ] **Step 2: Install the distro-provided `age` package if absent**

Run:

```bash
if ! command -v age >/dev/null 2>&1; then
  sudo apt-get update
  sudo apt-get install -y age
fi
age --version
```

Expected: `age` exits zero and prints a version. Do not download an unverified standalone binary.

- [ ] **Step 3: Prove the SSH identity can decrypt an `age` payload before generating the JKS**

Run this without shell tracing:

```bash
set -euo pipefail
umask 077
probe_dir="$(mktemp -d /tmp/termflow-age-probe.XXXXXX)"
trap 'find "$probe_dir" -type f -exec shred -u -- {} +; rmdir "$probe_dir"' EXIT
printf '%s' 'termflow-age-recovery-probe' > "$probe_dir/plain"
age -R /home/mcocdaa/.ssh/id_github.com.pub \
  -o "$probe_dir/plain.age" "$probe_dir/plain"
age -d -i /home/mcocdaa/.ssh/id_github.com \
  -o "$probe_dir/restored" "$probe_dir/plain.age"
cmp "$probe_dir/plain" "$probe_dir/restored"
```

Expected: `cmp` exits zero. If the private key has a passphrase, enter it only in the terminal prompt.

### Task 3: Generate, recover-test, and upload the Android signing identity atomically

**Files:**
- Create: `/home/mcocdaa/TermFlow-release-secrets/termflow-android-release-2026-08-13.tar.age`
- Modify externally: GitHub Actions repository secrets for `mcocdaa/TermFlow`
- Modify in repository: none

- [ ] **Step 1: Verify the permanent destination is new and lock down its parent directory**

Run:

```bash
set -euo pipefail
install -d -m 0700 /home/mcocdaa/TermFlow-release-secrets
test ! -e /home/mcocdaa/TermFlow-release-secrets/termflow-android-release-2026-08-13.tar.age
```

Expected: the destination does not already exist. Do not overwrite an existing recovery package.

- [ ] **Step 2: Run the locked-down bootstrap session**

Run the following as one shell session. The script intentionally emits only stage names, the public certificate fingerprint, and GitHub's secret-set acknowledgements.

```bash
set -euo pipefail
set +x
umask 077
export LC_ALL=C

repo='mcocdaa/TermFlow'
recipient='/home/mcocdaa/.ssh/id_github.com.pub'
identity='/home/mcocdaa/.ssh/id_github.com'
output_dir='/home/mcocdaa/TermFlow-release-secrets'
output="$output_dir/termflow-android-release-2026-08-13.tar.age"
work="$(mktemp -d /tmp/termflow-android-signing.XXXXXX)"
plain="$work/plain"
restored="$work/restored"
github_inputs="$work/github-inputs"

cleanup() {
  unset store_password key_password alias fingerprint
  if [[ -d "$work" ]]; then
    find "$work" -type f -exec shred -u -- {} +
    find "$work" -depth -type d -empty -delete
  fi
}
trap cleanup EXIT INT TERM

install -d -m 0700 "$plain" "$restored" "$github_inputs" "$output_dir"
test ! -e "$output"

alias="termflow-release-$(openssl rand -hex 8)"
store_password="$(openssl rand -hex 32)"
key_password="$(openssl rand -hex 32)"
keystore="$plain/termflow-android-release.jks"

echo 'Generating RSA-4096 Android release identity'
STOREPASS="$store_password" KEYPASS="$key_password" \
  keytool -genkeypair \
    -alias "$alias" \
    -keyalg RSA \
    -keysize 4096 \
    -sigalg SHA256withRSA \
    -validity 10000 \
    -dname 'CN=TermFlow Android Release, OU=TermFlow, O=TermFlow' \
    -keystore "$keystore" \
    -storetype JKS \
    -storepass:env STOREPASS \
    -keypass:env KEYPASS \
    >/dev/null

STOREPASS="$store_password" \
  keytool -list -v \
    -alias "$alias" \
    -keystore "$keystore" \
    -storetype JKS \
    -storepass:env STOREPASS \
    > "$work/keytool-list.txt"
grep -Fq 'Entry type: PrivateKeyEntry' "$work/keytool-list.txt"
grep -Fq 'Subject Public Key Algorithm: 4096-bit RSA key' "$work/keytool-list.txt"

STOREPASS="$store_password" \
  keytool -exportcert \
    -alias "$alias" \
    -keystore "$keystore" \
    -storetype JKS \
    -storepass:env STOREPASS \
    -file "$work/cert.der" \
    >/dev/null
fingerprint="$(
  openssl x509 -inform DER -in "$work/cert.der" -noout -fingerprint -sha256 \
    | cut -d= -f2 | tr -d ':' | tr 'a-f' 'A-F'
)"
[[ "$fingerprint" =~ ^[0-9A-F]{64}$ ]]

printf '%s\n' \
  'TermFlow Android release signing recovery package' \
  'Created: 2026-08-13' \
  'Keystore: termflow-android-release.jks' \
  'Store type: JKS' \
  'Key algorithm: RSA-4096' \
  "Certificate SHA-256: $fingerprint" \
  'Decrypt with:' \
  '  age -d -i ~/.ssh/id_github.com -o recovery.tar termflow-android-release-2026-08-13.tar.age' \
  'Then extract recovery.tar in a private temporary directory.' \
  > "$plain/README.txt"

printf '%s\n' \
  "ANDROID_KEYSTORE_PASSWORD=$store_password" \
  "ANDROID_KEY_ALIAS=$alias" \
  "ANDROID_KEY_PASSWORD=$key_password" \
  "ANDROID_SIGNING_CERT_SHA256=$fingerprint" \
  > "$plain/recovery.env"

tar -C "$plain" -cf "$work/recovery.tar" \
  termflow-android-release.jks README.txt recovery.env
age -R "$recipient" -o "$work/recovery.tar.age" "$work/recovery.tar"
chmod 0600 "$work/recovery.tar.age"

echo 'Proving encrypted recovery package'
age -d -i "$identity" -o "$work/restored.tar" "$work/recovery.tar.age"
tar -C "$restored" -xf "$work/restored.tar"
cmp "$keystore" "$restored/termflow-android-release.jks"
cmp "$plain/recovery.env" "$restored/recovery.env"

STOREPASS="$store_password" \
  keytool -exportcert \
    -alias "$alias" \
    -keystore "$restored/termflow-android-release.jks" \
    -storetype JKS \
    -storepass:env STOREPASS \
    -file "$work/restored-cert.der" \
    >/dev/null
restored_fingerprint="$(
  openssl x509 -inform DER -in "$work/restored-cert.der" -noout -fingerprint -sha256 \
    | cut -d= -f2 | tr -d ':' | tr 'a-f' 'A-F'
)"
test "$restored_fingerprint" = "$fingerprint"

install -m 0600 "$work/recovery.tar.age" "$output"
test "$(stat -c '%a' "$output")" = '600'

base64 -w 0 "$keystore" > "$github_inputs/ANDROID_KEYSTORE_BASE64"
printf '%s' "$store_password" > "$github_inputs/ANDROID_KEYSTORE_PASSWORD"
printf '%s' "$alias" > "$github_inputs/ANDROID_KEY_ALIAS"
printf '%s' "$key_password" > "$github_inputs/ANDROID_KEY_PASSWORD"
printf '%s' "$fingerprint" > "$github_inputs/ANDROID_SIGNING_CERT_SHA256"

echo 'Uploading five GitHub Actions secrets'
for name in \
  ANDROID_KEYSTORE_BASE64 \
  ANDROID_KEYSTORE_PASSWORD \
  ANDROID_KEY_ALIAS \
  ANDROID_KEY_PASSWORD \
  ANDROID_SIGNING_CERT_SHA256
do
  gh secret set "$name" --repo "$repo" < "$github_inputs/$name"
done

echo "Recovery package: $output"
echo "Public certificate SHA-256: $fingerprint"
```

Expected: the session exits zero, the `.age` file exists with mode `0600`, the recovered JKS matches byte-for-byte, and all five `gh secret set` calls succeed. No password or base64 keystore text appears in output.

- [ ] **Step 3: Verify the external state using metadata only**

Run:

```bash
stat -c 'mode=%a size=%s path=%n' \
  /home/mcocdaa/TermFlow-release-secrets/termflow-android-release-2026-08-13.tar.age
gh secret list --repo mcocdaa/TermFlow \
  | awk '$1 ~ /^ANDROID_(KEYSTORE_BASE64|KEYSTORE_PASSWORD|KEY_ALIAS|KEY_PASSWORD|SIGNING_CERT_SHA256)$/ {print $1, $2}' \
  | sort
```

Expected: the recovery file has mode `600`, and exactly the five required secret names appear with updated timestamps. GitHub will not reveal their values.

### Task 4: Dispatch and verify a signed Android candidate

**Files:**
- Read: `.github/workflows/tauri-packages.yml`
- Read: `scripts/release/verify_android_apk.py`
- Download temporarily: `termflow-android-arm64-apk`
- Modify in repository: none

- [ ] **Step 1: Confirm the remote workflow still has the signed-candidate path**

Run:

```bash
gh api repos/mcocdaa/TermFlow/contents/.github/workflows/tauri-packages.yml \
  --jq '.content' | base64 --decode \
  | grep -F 'signed_android_candidate'
```

Expected: the workflow contains the manual signed-candidate input and Android signing steps.

- [ ] **Step 2: Dispatch only Android and capture the new run ID**

Run:

```bash
set -euo pipefail
before_run="$(
  gh run list --repo mcocdaa/TermFlow \
    --workflow tauri-packages.yml --event workflow_dispatch --limit 1 \
    --json databaseId --jq '.[0].databaseId // 0'
)"

gh workflow run tauri-packages.yml \
  --repo mcocdaa/TermFlow \
  --ref main \
  -f platform=android \
  -f version=0.1.0-rc.5 \
  -f signed_android_candidate=true

run_id=''
for _ in $(seq 1 20); do
  candidate="$(
    gh run list --repo mcocdaa/TermFlow \
      --workflow tauri-packages.yml --event workflow_dispatch --limit 1 \
      --json databaseId --jq '.[0].databaseId // 0'
  )"
  if [[ "$candidate" != "$before_run" && "$candidate" != '0' ]]; then
    run_id="$candidate"
    break
  fi
  sleep 3
done
test -n "$run_id"
printf 'run_id=%s\n' "$run_id"
printf '%s' "$run_id" > /tmp/termflow-android-candidate-run-id
gh run view "$run_id" --repo mcocdaa/TermFlow \
  --json event,headBranch,headSha,status,url,workflowName
```

Expected: a new `workflow_dispatch` run on `main` is found. It is not a release/tag event.

- [ ] **Step 3: Wait for the candidate and inspect the Android job**

Run:

```bash
run_id="$(</tmp/termflow-android-candidate-run-id)"
[[ "$run_id" =~ ^[0-9]+$ ]]
gh run watch "$run_id" --repo mcocdaa/TermFlow --exit-status
gh run view "$run_id" --repo mcocdaa/TermFlow --json conclusion,jobs,url
```

Expected: `conclusion` is `success`; the Android job completes signing configuration, release APK build, `verify_android_apk.py`, secret cleanup, and artifact upload. A failure is evidence to diagnose, not permission to rotate the key automatically.

- [ ] **Step 4: Download the APK and independently compare its signer**

Run in a private temporary directory. Decryption may prompt for the SSH key passphrase; do not echo it.

```bash
set -euo pipefail
set +x
umask 077
candidate_dir="$(mktemp -d /tmp/termflow-android-candidate.XXXXXX)"
recovery_dir="$(mktemp -d /tmp/termflow-android-recovery-check.XXXXXX)"
run_id="$(</tmp/termflow-android-candidate-run-id)"
[[ "$run_id" =~ ^[0-9]+$ ]]
cleanup_candidate() {
  unset ANDROID_KEYSTORE_PASSWORD ANDROID_KEY_ALIAS ANDROID_KEY_PASSWORD \
    ANDROID_SIGNING_CERT_SHA256
  find "$recovery_dir" -type f -exec shred -u -- {} +
  find "$recovery_dir" -depth -type d -empty -delete
}
trap cleanup_candidate EXIT INT TERM

gh run download "$run_id" \
  --repo mcocdaa/TermFlow \
  --name termflow-android-arm64-apk \
  --dir "$candidate_dir"

age -d -i /home/mcocdaa/.ssh/id_github.com \
  -o "$recovery_dir/recovery.tar" \
  /home/mcocdaa/TermFlow-release-secrets/termflow-android-release-2026-08-13.tar.age
tar -C "$recovery_dir" -xf "$recovery_dir/recovery.tar" recovery.env
set -a
. "$recovery_dir/recovery.env"
set +a

apk="$(find "$candidate_dir" -type f -name '*.apk' -print -quit)"
test -n "$apk"
if command -v apksigner >/dev/null 2>&1; then
  apksigner="$(command -v apksigner)"
else
  sdk_root="${ANDROID_HOME:-${ANDROID_SDK_ROOT:-/usr/local/lib/android/sdk}}"
  apksigner="$(find "$sdk_root/build-tools" -type f -name apksigner -print 2>/dev/null | sort -V | tail -n 1)"
fi
if [[ -z "$apksigner" ]]; then
  sudo apt-get update
  sudo apt-get install -y apksigner
  apksigner="$(command -v apksigner)"
fi
test -x "$apksigner"

"$apksigner" verify --verbose --print-certs "$apk" > "$candidate_dir/apksigner.txt"
actual_fingerprint="$(
  sed -n 's/^Signer #1 certificate SHA-256 digest: //p' "$candidate_dir/apksigner.txt" \
    | head -n 1 | tr -d ':' | tr 'a-f' 'A-F'
)"
test "$actual_fingerprint" = "$ANDROID_SIGNING_CERT_SHA256"
sha256sum "$apk"
printf 'signer_sha256=%s\n' "$actual_fingerprint"
```

Expected: `apksigner verify` exits zero and its certificate SHA-256 exactly matches the fingerprint stored in the encrypted recovery package. The APK SHA-256 and run URL are safe evidence to report.

### Task 5: Record the handoff boundary

**Files:**
- Modify: none unless the user separately requests an evidence/runbook update

- [ ] **Step 1: Report the durable and live evidence**

Report only:

- recovery package path, mode, and encrypted file SHA-256;
- public signing certificate SHA-256;
- five configured secret names, never values;
- candidate Actions run URL and commit SHA;
- candidate APK filename, SHA-256, and signer match result;
- the explicit remaining task: copy the `.age` package to a second independent secure location.

- [ ] **Step 2: Confirm no repository release mutation occurred**

Run:

```bash
git status --short --branch
git rev-parse v0.1.0-rc.5
git ls-remote --tags origin refs/tags/v0.1.0-rc.5
```

Expected: no files containing key material are present, and the local and remote rc.5 tag IDs remain unchanged. Do not push, tag, or publish as part of this plan.
