# -*- coding: utf-8 -*-
"""
风格模仿模块 - 分析参考视频并提取剪辑风格特征
"""

import cv2
import numpy as np
from pathlib import Path

class StyleCopier:
    def __init__(self):
        self.style_features = {}
    
    def analyze_reference_video(self, video_path):
        """分析参考视频的剪辑风格"""
        print(f"分析参考视频风格：{video_path}")
        
        cap = cv2.VideoCapture(str(video_path))
        
        # 提取特征
        features = {
            'avg_shot_length': 0,
            'shot_lengths': [],
            'transition_types': [],
            'scene_cuts': []
        }
        
        prev_frame = None
        shot_start = 0
        frame_count = 0
        fps = cap.get(cv2.CAP_PROP_FPS)
        
        while True:
            ret, frame = cap.read()
            if not ret:
                break
            
            if prev_frame is not None:
                diff = self._calculate_frame_diff(prev_frame, frame)
                if diff > 30:  # 场景切换阈值
                    shot_length = (frame_count - shot_start) / fps
                    features['shot_lengths'].append(shot_length)
                    features['scene_cuts'].append(frame_count / fps)
                    shot_start = frame_count
            
            prev_frame = frame.copy()
            frame_count += 1
        
        cap.release()
        
        # 计算平均镜头长度
        if features['shot_lengths']:
            features['avg_shot_length'] = np.mean(features['shot_lengths'])
            features['std_shot_length'] = np.std(features['shot_lengths'])
        
        self.style_features = features
        return features
    
    def generate_style_suggestions(self, target_cuts):
        """根据参考风格生成目标视频剪辑建议"""
        if not self.style_features:
            return []
        
        suggestions = []
        avg_length = self.style_features.get('avg_shot_length', 3.0)
        
        for i in range(1, len(target_cuts)):
            current_length = target_cuts[i] - target_cuts[i-1]
            
            # 与参考风格对比
            if current_length > avg_length * 1.5:
                suggestions.append({
                    'type': 'pace_up',
                    'timestamp': target_cuts[i-1],
                    'reason': f'当前镜头 {current_length:.1f}s 比参考风格 ({avg_length:.1f}s) 长',
                    'suggested_length': avg_length
                })
            elif current_length < avg_length * 0.5:
                suggestions.append({
                    'type': 'merge',
                    'timestamp': target_cuts[i-1],
                    'reason': f'当前镜头 {current_length:.1f}s 太短，建议合并',
                    'suggested_length': avg_length
                })
        
        return suggestions
    
    def _calculate_frame_diff(self, frame1, frame2):
        """计算帧差异"""
        gray1 = cv2.cvtColor(frame1, cv2.COLOR_BGR2GRAY)
        gray2 = cv2.cvtColor(frame2, cv2.COLOR_BGR2GRAY)
        gray1 = cv2.resize(gray1, (100, 100))
        gray2 = cv2.resize(gray2, (100, 100))
        diff = cv2.absdiff(gray1, gray2)
        return np.mean(diff)
