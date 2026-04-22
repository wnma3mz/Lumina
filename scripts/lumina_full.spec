# -*- mode: python ; coding: utf-8 -*-
# Lumina Full 打包 spec（固定路径，不在构建时重新生成，确保 PyInstaller 缓存生效）
from pathlib import Path
from PyInstaller.utils.hooks import collect_all, collect_submodules

# SPECPATH 是 spec 文件所在目录（scripts/），project_dir 是其父目录
project_dir = Path(SPECPATH).parent

# collect_all 确保 mlx / mlx_lm / mlx_whisper 的子模块和数据文件完整打包
mlx_datas, mlx_bins, mlx_hidden     = collect_all('mlx')
mlx_lm_datas, mlx_lm_bins, mlx_lm_hidden = collect_all('mlx_lm')
mlx_wh_datas, mlx_wh_bins, mlx_wh_hidden = collect_all('mlx_whisper')

# mlx.core 的 rpath 是 @loader_path/lib，collect_all 会把 libmlx.dylib 提升到顶层破坏路径
# 解决：显式放到 mlx/lib/ 目标目录
import mlx.core as _mlx_core
_mlx_lib_src = Path(_mlx_core.__file__).parent / 'lib'
_mlx_extra_binaries = [(str(_mlx_lib_src / 'libmlx.dylib'), 'mlx/lib')]
_mlx_extra_datas = [
    (str(_mlx_lib_src / 'mlx.metallib'), 'mlx'),
    (str(_mlx_lib_src / 'mlx.metallib'), 'mlx/lib'),
]
mlx_bins = [(src, dst) for src, dst in mlx_bins if 'libmlx.dylib' not in src]

a = Analysis(
    [str(project_dir / 'lumina' / 'main.py')],
    pathex=[str(project_dir)],
    binaries=mlx_bins + mlx_lm_bins + mlx_wh_bins + _mlx_extra_binaries,
    datas=(
        mlx_datas + mlx_lm_datas + mlx_wh_datas
        + _mlx_extra_datas
        + [
            (str(project_dir / 'lumina' / 'config.json'), 'lumina'),
            (str(project_dir / 'assets' / 'lumina.icns'), 'assets'),
            (str(project_dir / 'lumina' / 'api' / 'static'), 'lumina/api/static'),
            (str(project_dir / 'scripts' / 'install_quick_action.sh'), 'scripts'),
        ]
    ),
    hiddenimports=sorted(set(
        mlx_hidden + mlx_lm_hidden + mlx_wh_hidden
        + collect_submodules('mlx')
        + collect_submodules('mlx_lm')
        + collect_submodules('mlx_whisper')
        + [
            'sounddevice', 'scipy',
            'fastapi', 'uvicorn', 'uvicorn.logging',
            'transformers', 'huggingface_hub',
            'aiohttp',
            'pdf2zh',
            'rumps',
        ]
    )),
    hookspath=[],
    runtime_hooks=[str(project_dir / 'build' / 'rthook_edition_full.py')],
    excludes=[
        # torch：mlx-whisper 的 metadata 依赖，Apple Silicon 运行时不需要
        # 排除节省 ~356MB 体积 + ~8s Analysis 时间
        'torch', 'torchvision', 'torchaudio',
        'pytest',
        'IPython', 'jupyter', 'notebook', 'ipykernel',
        'tkinter', '_tkinter', 'wx', 'gi',
        'black', 'isort', 'mypy', 'pylint', 'flake8',
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='lumina',
    debug=False,
    strip=False,
    upx=False,
    console=True,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name='lumina-full',
)

app = BUNDLE(
    coll,
    name='Lumina.app',
    icon=str(project_dir / 'assets' / 'lumina.icns'),
    bundle_identifier='com.lumina.server',
    info_plist={
        'CFBundleShortVersionString': '0.8.6',
        'CFBundleName': 'Lumina',
        'LSUIElement': True,
        'NSMicrophoneUsageDescription': 'Lumina 需要麦克风权限用于语音转文本',
    },
)
