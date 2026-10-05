@echo off
rem TransPDF 一键构建：安装依赖 -> 准备字体 -> 打包 exe
cd /d %~dp0

echo [1/3] 安装依赖...
python -m pip install -r requirements.txt || goto :err

echo [2/3] 准备中文字体（TTF 轮廓）...
if not exist fonts\SourceHanSansCN-Regular.ttf (
    if not exist fonts\SourceHanSansCN-Regular.otf (
        echo 下载思源黑体...
        python -c "import urllib.request; urllib.request.urlretrieve('https://cdn.jsdelivr.net/gh/adobe-fonts/source-han-sans@release/SubsetOTF/CN/SourceHanSansCN-Regular.otf', 'fonts/SourceHanSansCN-Regular.otf')"
    )
    if exist fonts\SourceHanSansCN-Regular.otf (
        python tools\otf2ttf.py fonts\SourceHanSansCN-Regular.otf fonts\SourceHanSansCN-Regular.ttf
    )
)

echo [3/3] PyInstaller 打包...
python -m PyInstaller --noconfirm --clean --onefile --windowed --name TransPDF --add-data "fonts;fonts" main.py || goto :err

echo.
echo 构建完成: dist\TransPDF.exe
echo 运行自检: dist\TransPDF.exe --selftest
echo 组装发布包: python tools\assemble_release.py --llama <llama.cpp目录> --model <模型.gguf>
goto :eof

:err
echo 构建失败，请检查上方错误信息。
exit /b 1
