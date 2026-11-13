import sys
import os

def test_dependencies():
    print('=' * 50)
    print('测试依赖包安装')
    print('=' * 50)
    
    tests = [
        ('opencv', 'cv2'),
        ('numpy', 'numpy'),
        ('moviepy', 'moviepy'),
        ('pydub', 'pydub'),
        ('torch', 'torch'),
        ('scikit-learn', 'sklearn'),
        ('yt-dlp', 'yt_dlp'),
        ('Pillow', 'PIL'),
    ]
    
    for name, module in tests:
        try:
            __import__(module)
            print('[OK] ' + name)
        except ImportError as e:
            print('[FAIL] ' + name + ': ' + str(e))

def test_ffmpeg():
    print('\n' + '=' * 50)
    print('测试 FFmpeg')
    print('=' * 50)
    
    try:
        import imageio_ffmpeg
        ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()
        print('[OK] FFmpeg 路径：' + ffmpeg_path)
        return True
    except Exception as e:
        print('[FAIL] FFmpeg 错误：' + str(e))
        return False

def test_modules():
    print('\n' + '=' * 50)
    print('测试项目模块')
    print('=' * 50)
    
    try:
        from backend.video_processor import VideoProcessor
        print('[OK] video_processor')
    except Exception as e:
        print('[FAIL] video_processor: ' + str(e))
    
    try:
        from backend.ai_analyzer import AIAnalyzer
        print('[OK] ai_analyzer')
    except Exception as e:
        print('[FAIL] ai_analyzer: ' + str(e))
    
    try:
        from backend.style_copier import StyleCopier
        print('[OK] style_copier')
    except Exception as e:
        print('[FAIL] style_copier: ' + str(e))
    
    try:
        from utils.downloader import VideoDownloader
        print('[OK] downloader')
    except Exception as e:
        print('[FAIL] downloader: ' + str(e))

    try:
        from backend.autocut import decide, STRENGTHS
        print('[OK] autocut (力度档位: %s)' % ' / '.join(k['label'] for k in STRENGTHS.values()))
    except Exception as e:
        print('[FAIL] autocut: ' + str(e))

    try:
        from backend.exporter import export_kit, write_edl
        print('[OK] exporter')
    except Exception as e:
        print('[FAIL] exporter: ' + str(e))

    try:
        from backend.server import app
        print('[OK] server')
    except Exception as e:
        print('[FAIL] server: ' + str(e))

def main():
    print('\n' + '#' * 50)
    print('# AI 剪辑工具 - 功能测试')
    print('#' * 50 + '\n')
    
    test_dependencies()
    test_ffmpeg()
    test_modules()
    
    print('\n' + '=' * 50)
    print('测试完成！')
    print('=' * 50)

if __name__ == '__main__':
    main()
