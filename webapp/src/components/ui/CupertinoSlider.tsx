import React, { useRef, useState, useCallback } from 'react';
import { telegram } from '../../services/telegram';

interface CupertinoSliderProps {
  value: number;
  min: number;
  max: number;
  step?: number;
  onChange: (val: number) => void;
  icon?: React.ReactNode;
  label?: string;
  displayValue?: string | number;
  unit?: string;
}

export const CupertinoSlider: React.FC<CupertinoSliderProps> = ({
  value,
  min,
  max,
  step = 1,
  onChange,
  icon,
  label,
  displayValue,
  unit = '',
}) => {
  const trackRef = useRef<HTMLDivElement>(null);
  const rectRef = useRef<DOMRect | null>(null);
  const lastHapticRef = useRef<number>(0);
  const [isDragging, setIsDragging] = useState(false);

  // Normalized clamped percentage [0, 100]
  const percentage = Math.min(100, Math.max(0, ((value - min) / (max - min)) * 100));

  const updatePosition = useCallback((clientX: number) => {
    const rect = rectRef.current || trackRef.current?.getBoundingClientRect();
    if (!rect) return;
    
    const pos = Math.min(Math.max(0, clientX - rect.left), rect.width);
    const rawVal = min + (pos / rect.width) * (max - min);
    const steppedVal = Math.round(rawVal / step) * step;
    const clamped = Math.min(max, Math.max(min, steppedVal));

    if (clamped !== value) {
      const now = performance.now();
      // Throttle haptics to min 70ms cooldown to protect Android Telegram bridge from freezing
      if (now - lastHapticRef.current > 70) {
        lastHapticRef.current = now;
        telegram.impact('light');
      }
      onChange(clamped);
    }
  }, [min, max, step, value, onChange]);

  const handlePointerDown = (e: React.PointerEvent<HTMLDivElement>) => {
    if (!trackRef.current) return;
    e.currentTarget.setPointerCapture(e.pointerId);
    rectRef.current = trackRef.current.getBoundingClientRect();
    setIsDragging(true);
    telegram.impact('medium');
    updatePosition(e.clientX);
  };

  const handlePointerMove = (e: React.PointerEvent<HTMLDivElement>) => {
    if (isDragging || e.buttons === 1) {
      updatePosition(e.clientX);
    }
  };

  const handlePointerEnd = (e: React.PointerEvent<HTMLDivElement>) => {
    if (e.currentTarget.hasPointerCapture(e.pointerId)) {
      e.currentTarget.releasePointerCapture(e.pointerId);
    }
    if (isDragging) {
      setIsDragging(false);
      rectRef.current = null;
      telegram.impact('light');
    }
  };

  return (
    <div className="flex flex-col gap-2 w-full select-none" style={{ contain: 'layout style' }}>
      {(label || displayValue) && (
        <div className="flex items-center justify-between text-xs px-0.5">
          <span className="font-semibold text-white/90 flex items-center gap-1.5 tracking-tight">
            {icon}
            {label}
          </span>
          <span className="font-mono text-[#64D2FF] font-bold tabular-nums bg-white/[0.08] px-2 py-0.5 rounded-full border border-white/10 shadow-sm">
            {displayValue ?? `${value}${unit}`}
          </span>
        </div>
      )}

      {/* 120 FPS Liquid Vision Capsule Slider */}
      <div className="relative pt-6 pb-1">
        {/* Floating 3D Value Bubble (Pops up with spring physics when dragging) */}
        <div
          className={`absolute -top-1 pointer-events-none transition-all duration-200 z-30 flex flex-col items-center ${
            isDragging ? 'opacity-100 scale-105 -translate-y-2' : 'opacity-0 scale-75 translate-y-1'
          }`}
          style={{
            left: `calc(14px + (100% - 28px) * ${percentage / 100})`,
            transform: 'translateX(-50%)',
            transition: isDragging ? 'opacity 0.15s ease-out, transform 0.15s cubic-bezier(0.34, 1.56, 0.64, 1)' : 'all 0.2s ease-in',
          }}
        >
          <div className="px-2.5 py-1 rounded-full bg-gradient-to-r from-[#0A84FF] to-[#64D2FF] text-white text-[11px] font-bold font-mono shadow-[0_4px_14px_rgba(10,132,255,0.6)] border border-white/40 whitespace-nowrap">
            {displayValue ?? `${value}${unit}`}
          </div>
          {/* Arrow notch */}
          <div className="w-1.5 h-1.5 bg-[#64D2FF] rotate-45 -mt-1 shadow-sm" />
        </div>

        {/* Capsule Track */}
        <div
          ref={trackRef}
          touch-action="none"
          style={{
            touchAction: 'none',
            transform: 'translate3d(0,0,0)',
            backfaceVisibility: 'hidden',
          }}
          onPointerDown={handlePointerDown}
          onPointerMove={handlePointerMove}
          onPointerUp={handlePointerEnd}
          onPointerCancel={handlePointerEnd}
          className={`relative h-10 w-full rounded-full bg-black/40 border border-white/[0.14] flex items-center p-1 cursor-pointer overflow-hidden backdrop-blur-md transition-shadow ${
            isDragging ? 'border-[#64D2FF]/60 shadow-[0_0_20px_rgba(10,132,255,0.3)]' : 'hover:border-white/25'
          }`}
        >
          {/* Milestone Background Detents (25%, 50%, 75%) */}
          <div className="absolute inset-0 flex justify-between items-center px-6 pointer-events-none z-0 opacity-20">
            <div className="w-1 h-2 rounded-full bg-white" />
            <div className="w-1 h-3 rounded-full bg-white" />
            <div className="w-1 h-2 rounded-full bg-white" />
          </div>

          {/* Filled Liquid Active Track */}
          <div
            className="h-full rounded-full bg-gradient-to-r from-[#0071E3] via-[#0A84FF] to-[#64D2FF] relative z-10 shadow-[0_0_12px_rgba(10,132,255,0.5)]"
            style={{
              width: `${percentage}%`,
              transition: isDragging ? 'none' : 'width 0.25s cubic-bezier(0.34, 1.56, 0.64, 1)',
              transform: 'translate3d(0,0,0)',
              willChange: isDragging ? 'width' : 'auto',
            }}
          >
            {/* Shimmer line inside liquid fill */}
            <div className="absolute inset-y-0 right-0 w-2 bg-white/40 blur-[1px] rounded-r-full" />
          </div>

          {/* Magnetic Knob Thumb */}
          <div
            className="absolute top-1 bottom-1 w-8 h-8 rounded-full pointer-events-none z-20 flex items-center justify-center"
            style={{
              left: `calc(4px + (100% - 40px) * ${percentage / 100})`,
              transition: isDragging ? 'none' : 'left 0.25s cubic-bezier(0.34, 1.56, 0.64, 1)',
              transform: 'translate3d(0,0,0)',
              willChange: isDragging ? 'left' : 'auto',
            }}
          >
            <div
              className={`w-full h-full rounded-full bg-white shadow-[0_2px_12px_rgba(0,0,0,0.6),0_0_0_1px_rgba(255,255,255,0.9)] flex items-center justify-center transition-transform ${
                isDragging ? 'scale-115 shadow-[0_0_20px_rgba(100,210,255,0.8)]' : 'scale-100'
              }`}
            >
              {/* Inner glowing core */}
              <div
                className={`rounded-full transition-all ${
                  isDragging ? 'w-2.5 h-2.5 bg-[#0A84FF]' : 'w-2 h-2 bg-black/30'
                }`}
              />
            </div>
          </div>
        </div>
      </div>
    </div>
  );
};
