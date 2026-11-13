# -*- coding: utf-8 -*-
"""
测试脚本 - 快速验证核心功能
"""

import sys
from pathlib import Path

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent))

from backend.video_processor import VideoProcessor
from backend.ai_analyzer import AIAnalyzer

def test_video_analysis(video_path):
    """测试视频分析功能"""
    print("=" * 50)
    print("AI 剪辑工具 - 功能测试")
    print("=" * 50)
    
    # 1. 测试视频处理器
    print("\n1. 测试视频信息获取...")
    processor = VideoProcessor(video_path)
    info = processor.get_video_info()
    print(f"   ✓ 视频时长：{info['duration']:.2f} 秒")
    print(f"   ✓ 视频分辨率：{info['width']}x{info['height']}")
    print(f"   ✓ 帧率：{info['fps']:.2f} fps")
    
    # 2. 测试场景检测
    print("\n2. 测试场景切换检测...")
    cuts = processor.detect_scene_cuts()
    print(f"   ✓ 检测到 {len(cuts)} 个场景切换点")
    
    if cuts:
        print("\n   场景切换时间点:")
        for i, cut in enumerate(cuts[:10]):  # 只显示前 10 个
            print(f"   {i+1}. {cut['timestamp']:.2f}s (置信度：{cut['confidence']:.2f})")
        if len(cuts) > 10:
            print(f"   ... 还有 {len(cuts)-10} 个切换点")
    
    # 3. 测试 AI 分析
    print("\n3. 测试 AI 剪辑建议生成...")
    analyzer = AIAnalyzer(video_path)
    result = analyzer.analyze()
    print(f"   ✓ 生成 {len(result['suggestions'])} 条剪辑建议")
    
    print("\n" + "=" * 50)
    print("测试完成！")
    print("=" * 50)

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("使用方法：python test.py <视频路径>")
        print("示例：python test.py sample.mp4")
    else:
        test_video_analysis(sys.argv[1])
