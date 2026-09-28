"""配置 iOS 发布签名，供 GitHub Actions 的 ios 任务使用。

对齐 configure_signing.py 的约定：未配置 Secrets 时不报错，只提示并退出 0，
让构建继续走未签名 IPA 路径。配置齐全时会生成 ios/ExportOptions.plist，
build_ios.py 检测到该文件即自动改用 flutter build ipa 导出已签名 IPA。
"""
import argparse
import base64
import json
import os
import platform
import plistlib
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
keychain_name = 'duanju-ios.keychain-db'
keychain_password = 'duanju-ios'
state_name = 'duanju-ios-signing.json'
export_options = root / 'ios' / 'ExportOptions.plist'
required_secrets = ['IOS_CERTIFICATE_BASE64', 'IOS_CERTIFICATE_PASSWORD',
                    'IOS_PROVISIONING_PROFILE_BASE64']


def run(arguments, **kwargs):
    return subprocess.run(arguments, check=True, capture_output=True, text=True, **kwargs)


def decode(name):
    raw = os.environ.get(name, '')
    try:
        data = base64.b64decode(''.join(raw.split()), validate=True)
    except ValueError:
        raise SystemExit(name + ' 不是有效的 Base64。')
    if not data:
        raise SystemExit(name + ' 解码后为空。')
    return data


def read_profile(path):
    """解析 .mobileprovision，取出描述文件名与 App ID 前缀。"""
    try:
        stripped = run(['security', 'cms', '-D', '-i', str(path)]).stdout
        profile = plistlib.loads(stripped.encode('utf-8'))
    except (subprocess.CalledProcessError, plistlib.InvalidFileException, ValueError):
        raise SystemExit('描述文件无法解析，请确认是有效的 .mobileprovision。')
    name = str(profile.get('Name') or '').strip()
    identifier = str((profile.get('Entitlements') or {}).get('application-identifier') or '').strip()
    team, separator, bundle = identifier.partition('.')
    if not name or not separator or not team or not bundle:
        raise SystemExit('描述文件缺少 Name 或 application-identifier。')
    return name, team, bundle, str(profile.get('UUID') or '').strip()


def configure():
    runner_temp = os.environ.get('RUNNER_TEMP')
    if not runner_temp:
        raise SystemExit('此脚本用于 GitHub Actions；本机构建请直接用 --export-options 指定自己的配置。')
    temp = Path(runner_temp)
    certificate = temp / 'duanju-ios.p12'
    certificate.write_bytes(decode('IOS_CERTIFICATE_BASE64'))
    certificate.chmod(0o600)

    profiles = Path.home() / 'Library' / 'MobileDevice' / 'Provisioning Profiles'
    profiles.mkdir(parents=True, exist_ok=True)
    staged_profile = temp / 'duanju-ios.mobileprovision'
    staged_profile.write_bytes(decode('IOS_PROVISIONING_PROFILE_BASE64'))
    name, team, bundle, identifier = read_profile(staged_profile)
    installed_profile = profiles / (identifier or 'duanju-ios').lower().replace(' ', '-')
    installed_profile = installed_profile.with_suffix('.mobileprovision')
    installed_profile.write_bytes(staged_profile.read_bytes())
    staged_profile.unlink()

    keychain = temp / keychain_name
    if keychain.exists():
        run(['security', 'delete-keychain', str(keychain)])
    run(['security', 'create-keychain', '-p', keychain_password, str(keychain)])
    run(['security', 'set-keychain-settings', '-lut', '21600', str(keychain)])
    run(['security', 'unlock-keychain', '-p', keychain_password, str(keychain)])
    run(['security', 'import', str(certificate), '-k', str(keychain),
         '-P', os.environ['IOS_CERTIFICATE_PASSWORD'], '-A', '-t', 'cert', '-f', 'pkcs12'])
    run(['security', 'set-key-partition-list', '-S', 'apple-tool:,apple:,codesign:',
         '-s', '-k', keychain_password, str(keychain)])
    existing = run(['security', 'list-keychains', '-d', 'user']).stdout
    run(['security', 'list-keychains', '-d', 'user', '-s', str(keychain), *existing.split()])

    export_options.parent.mkdir(parents=True, exist_ok=True)
    with export_options.open('wb') as stream:
        plistlib.dump({
            'method': os.environ.get('IOS_EXPORT_METHOD', '').strip() or 'ad-hoc',
            'teamID': team,
            'signingStyle': 'manual',
            'provisioningProfiles': {bundle: name},
            'stripSwiftSymbols': True,
            'compileBitcode': False,
            'uploadSymbols': False,
        }, stream)
    (temp / state_name).write_text(json.dumps({
        'keychain': str(keychain), 'certificate': str(certificate),
        'profile': str(installed_profile),
    }), encoding='utf-8')
    print(f'已配置 iOS 发布签名：{name}（{bundle}，导出方式 {os.environ.get("IOS_EXPORT_METHOD", "").strip() or "ad-hoc"}）')


def clean():
    runner_temp = os.environ.get('RUNNER_TEMP')
    export_options.unlink(missing_ok=True)
    if not runner_temp:
        return
    state = Path(runner_temp) / state_name
    if not state.is_file():
        return
    try:
        recorded = json.loads(state.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        recorded = {}
    for name in ('keychain', 'certificate', 'profile'):
        target = recorded.get(name)
        if not target:
            continue
        path = Path(target)
        if name == 'keychain' and path.exists():
            subprocess.run(['security', 'delete-keychain', str(path)], check=False,
                           capture_output=True, text=True)
        else:
            path.unlink(missing_ok=True)
    state.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description='配置 iOS 发布签名（GitHub Actions）')
    parser.add_argument('--clean', action='store_true')
    options = parser.parse_args()
    if platform.system() != 'Darwin':
        raise SystemExit('iOS 签名配置需要 macOS。')
    if options.clean:
        clean()
        return 0
    if not any(os.environ.get(name, '').strip() for name in required_secrets + ['IOS_EXPORT_METHOD']):
        print('未配置 iOS 签名 Secrets：生成未签名 IPA；正式发布请配置证书与描述文件。')
        return 0
    missing = [name for name in required_secrets if not os.environ.get(name, '').strip()]
    if missing:
        raise SystemExit('iOS 签名需要同时配置全部必需 Secrets：' + '、'.join(missing))
    configure()
    return 0


if __name__ == '__main__':
    sys.exit(main())
