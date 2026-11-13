# -*- coding: utf-8 -*-
"""
视频下载工具 - 支持在线视频链接下载
"""

import yt_dlp
from pathlib import Path

class VideoDownloader:
    def __init__(self, output_dir="projects"):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(exist_ok=True)
    
    def download_from_url(self, url, filename=None):
        """从 URL 下载视频"""
        print(f"正在下载视频：{url}")
        
        ydl_opts = {
            'outtmpl': str(self.output_dir / (filename or '%(title)s.%(ext)s')),
            'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best',
            'quiet': False,
            'no_warnings': True
        }

        # yt-dlp 合并音视频需要 ffmpeg，指向 imageio-ffmpeg 自带的那份
        try:
            from utils.ffmpeg_setup import ffmpeg_dir
            location = ffmpeg_dir()
            if location:
                ydl_opts['ffmpeg_location'] = location
        except Exception:
            pass
        
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                return {
                    'success': True,
                    'path': str(self.output_dir / f"{info.get('title', 'video')}.mp4"),
                    'title': info.get('title', ''),
                    'duration': info.get('duration', 0)
                }
        except Exception as e:
            return {
                'success': False,
                'error': str(e)
            }
    
    def get_video_info(self, url):
        """获取视频信息（不下载）"""
        ydl_opts = {
            'quiet': True,
            'no_warnings': True
        }
        
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                return {
                    'title': info.get('title', ''),
                    'duration': info.get('duration', 0),
                    'uploader': info.get('uploader', ''),
                    'view_count': info.get('view_count', 0)
                }
        except Exception as e:
            return {'error': str(e)}
