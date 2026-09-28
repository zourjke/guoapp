import argparse
import hashlib
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from app_build import BuildVariant, add_variant_argument

root = Path(__file__).resolve().parents[1]
default_export_options = root / 'ios' / 'ExportOptions.plist'


def run(arguments, **kwargs):
    subprocess.run(arguments, cwd=kwargs.pop('cwd', root), check=True, **kwargs)


def build_core(simulator=False, variant=BuildVariant()):
    if platform.system() != 'Darwin':
        raise SystemExit('iOS 构建需要 macOS 和完整 Xcode。')
    go = shutil.which('go')
    if not go:
        raise SystemExit('请安装 Go 1.24.1 或更新版本。')
    environment = os.environ.copy()
    environment.setdefault('GOPROXY', 'https://goproxy.cn,direct')
    environment.setdefault('GOSUMDB', 'off')
    environment['CGO_ENABLED'] = '1'
    environment['GOTOOLCHAIN'] = os.environ.get('GOTOOLCHAIN', 'auto')
    slices = [('iphoneos', 'arm64', 'arm64-apple-ios15.1')]
    if simulator:
        slices.extend([
            ('iphonesimulator', 'arm64', 'arm64-apple-ios15.1-simulator'),
            ('iphonesimulator', 'amd64', 'x86_64-apple-ios15.1-simulator'),
        ])
    libraries = []
    for sdk, architecture, triple in slices:
        sdk_path = subprocess.check_output(['xcrun', '--sdk', sdk, '--show-sdk-path'], text=True).strip()
        compiler = subprocess.check_output(['xcrun', '--sdk', sdk, '--find', 'clang'], text=True).strip()
        directory = root / 'native' / 'build' / 'ios' / f'{sdk}-{architecture}'
        headers = directory / 'Headers'
        headers.mkdir(parents=True, exist_ok=True)
        output = directory / 'libDuanjuCore.a'
        flags = shlex.join(['-isysroot', sdk_path, '-target', triple])
        build_env = environment | {
            'GOOS': 'ios', 'GOARCH': architecture, 'CC': compiler,
            'CGO_CFLAGS': flags, 'CGO_LDFLAGS': flags,
        }
        run([go, 'build', '-trimpath', '-buildmode=c-archive', '-ldflags=' + variant.linker_flags,
             '-o', str(output), './bridge'], cwd=root / 'native', env=build_env)
        shutil.copy2(output.with_suffix('.h'), headers / 'DuanjuCore.h')
        libraries.append((sdk, output, headers))
    arguments = ['xcodebuild', '-create-xcframework']
    device = libraries[0]
    arguments.extend(['-library', str(device[1]), '-headers', str(device[2])])
    if simulator:
        universal = root / 'native' / 'build' / 'ios' / 'simulator-universal'
        universal.mkdir(parents=True, exist_ok=True)
        archive = universal / 'libDuanjuCore.a'
        run(['xcrun', 'lipo', '-create', str(libraries[1][1]), str(libraries[2][1]), '-output', str(archive)])
        arguments.extend(['-library', str(archive), '-headers', str(libraries[1][2])])
    framework = root / 'ios' / 'DuanjuCore' / 'DuanjuCore.xcframework'
    if framework.exists():
        shutil.rmtree(framework)
    run(arguments + ['-output', str(framework)])


def stage_payload(application, staging):
    """把 .app 放进 staging/Payload/ 下，返回 Payload 目录。

    IPA 的目录结构必须是 Payload/<App>.app，否则侧载工具无法识别。
    """
    if staging.exists():
        shutil.rmtree(staging)
    payload = staging / 'Payload'
    payload.mkdir(parents=True)
    shutil.copytree(application, payload / application.name, symlinks=True)
    return payload


def package_unsigned_ipa(application, destination, archiver=None):
    """把未签名的 .app 打包成未签名 .ipa。

    用 ditto 而非 zip：ditto 会保留 .app 内 framework 的符号链接，
    丢掉链接会导致侧载重签名后应用无法启动。
    """
    archiver = archiver or run
    staging = destination.parent / (destination.stem + '.payload')
    try:
        payload = stage_payload(application, staging)
        archiver(['ditto', '-c', '-k', '--sequesterRsrc', '--keepParent', str(payload), str(destination)])
    finally:
        if staging.exists():
            shutil.rmtree(staging)
    if not destination.is_file() or destination.stat().st_size == 0:
        raise SystemExit('未生成未签名 IPA：' + str(destination))
    return destination


def main():
    parser = argparse.ArgumentParser(description='构建红果鉴 / 真果鉴 iOS 核心和应用')
    parser.add_argument('--core-only', action='store_true')
    parser.add_argument('--simulator', action='store_true', help='额外生成模拟器核心；不启动模拟器')
    parser.add_argument('--export-options', type=Path,
                        help='使用自己的 Xcode 签名配置导出 IPA；省略时自动使用 ios/ExportOptions.plist')
    add_variant_argument(parser)
    options = parser.parse_args()
    variant = BuildVariant(options.all_sources)
    build_core(options.simulator, variant)
    if options.core_only:
        return
    flutter = shutil.which('flutter')
    if not flutter or not shutil.which('pod'):
        raise SystemExit('请安装 Flutter 和 CocoaPods。')
    run([flutter, 'pub', 'get', '--enforce-lockfile'])
    run(['pod', 'install'], cwd=root / 'ios')
    output = root / 'dist' / 'ios'
    output.mkdir(parents=True, exist_ok=True)
    version = re.search(r'^version:\s*(\S+)', (root / 'pubspec.yaml').read_text(), re.MULTILINE).group(1)
    artifacts = []
    export_options = options.export_options
    if export_options is None and default_export_options.is_file():
        export_options = default_export_options
        print('检测到 iOS 签名配置，导出已签名 IPA。')
    if export_options:
        config = export_options.expanduser().resolve()
        if not config.is_file():
            raise SystemExit('ExportOptions.plist 不存在。')
        run([flutter, 'build', 'ipa', '--release', '--no-pub', '--export-options-plist', str(config), *variant.flutter_arguments])
        for package in (root / 'build' / 'ios' / 'ipa').glob('*.ipa'):
            destination = output / f'{variant.slug}-{version}-ios.ipa'
            shutil.copy2(package, destination)
            artifacts.append(destination)
    else:
        run([flutter, 'build', 'ios', '--release', '--no-codesign', '--no-pub', *variant.flutter_arguments])
        application = root / 'build' / 'ios' / 'iphoneos' / 'Runner.app'
        if not application.is_dir():
            raise SystemExit('未生成 iOS 应用：' + str(application))
        symbols = subprocess.check_output(['xcrun', 'nm', '-gU', str(application / 'Runner')], text=True)
        for symbol in ['_DuanjuRequest', '_DuanjuFree']:
            if symbol not in symbols:
                raise SystemExit('iOS 包缺少 FFI 入口：' + symbol)
        archive = output / f'{variant.slug}-{version}-ios-unsigned-app.zip'
        run(['ditto', '-c', '-k', '--sequesterRsrc', '--keepParent', str(application), str(archive)])
        artifacts.append(archive)
        ipa = output / f'{variant.slug}-{version}-ios-unsigned.ipa'
        artifacts.append(package_unsigned_ipa(application, ipa))
    if not artifacts:
        raise SystemExit('未生成 iOS 安装包。')
    checksums = []
    for artifact in sorted(output.glob(f'*-{version}-ios*')):
        digest = hashlib.sha256()
        with artifact.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(chunk)
        checksums.append(f'{digest.hexdigest()}  {artifact.name}')
        print(artifact)
    (output / 'SHA256SUMS.txt').write_text('\n'.join(checksums) + '\n', encoding='ascii')


if __name__ == '__main__':
    main()
