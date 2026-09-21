import React, { useRef, useState } from 'react';
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
}) => {
  const trackRef = useRef<HTMLDivElement>(null);
  const [isDragging, setIsDragging] = useState(false);

  // Normalized clamped percentage [0, 100]
  const percentage = Math.min(100, Math.max(0, ((value - min) / (max - min)) * 100));

  const handlePointer = (e: React.PointerEvent<HTMLDivElement>) => {
    if (!trackRef.current) return;
    const rect = trackRef.current.getBoundingClientRect();
    const pos = Math.min(Math.max(0, e.clientX - rect.left), rect.width);
    const rawVal = min + (pos / rect.width) * (max - min);
    const steppedVal = Math.round(rawVal / step) * step;
    const clamped = Math.min(max, Math.max(min, steppedVal));
    if (clamped !== value) {
      telegram.impact('light');
      onChange(clamped);
    }
  };

  return (
    <div className="flex flex-col gap-1.5 w-full select-none">
      {(label || displayValue) && (
        <div className="flex items-center justify-between text-xs px-0.5">
          <span className="font-semibold text-white/90 flex items-center gap-1.5 tracking-tight">
            {icon}
            {label}
          </span>
          <span className="font-mono text-[#64D2FF] font-bold tabular-nums">
            {displayValue ?? value}
          </span>
        </div>
      )}

      {/* visionOS Capsule Track (From Images 3 & 4) */}
      <div
        ref={trackRef}
        touch-action="none"
        style={{ touchAction: 'none' }}
        onPointerDown={(e) => {
          e.currentTarget.setPointerCapture(e.pointerId);
          setIsDragging(true);
          handlePointer(e);
        }}
        onPointerMove={(e) => {
          if (isDragging || e.buttons === 1) handlePointer(e);
        }}
        onPointerUp={() => setIsDragging(false)}
        onPointerCancel={() => setIsDragging(false)}
        className={`relative h-9 w-full rounded-full bg-white/[0.08] border border-white/[0.14] flex items-center p-1 cursor-pointer overflow-hidden backdrop-blur-2xl transition-all ${
          isDragging ? 'bg-white/[0.14] border-white/25 shadow-inner' : 'hover:bg-white/[0.11]'
        }`}
      >
        {/* Filled active track */}
        <div
          className="h-full rounded-full bg-gradient-to-r from-[#0A84FF] to-[#64D2FF] transition-all ease-out"
          style={{ width: `${percentage}%` }}
        />

        {/* Knob circle bounded safely inside the 4px padding container */}
        <div
          className={`absolute top-1 bottom-1 w-7 h-7 rounded-full bg-white shadow-[0_2px_10px_rgba(0,0,0,0.50),0_0_0_1px_rgba(255,255,255,0.8)] pointer-events-none transition-transform ease-out flex items-center justify-center ${
            isDragging ? 'scale-110' : 'scale-100'
          }`}
          style={{
            left: `calc(4px + (100% - 36px) * ${percentage / 100})`,
            transition: isDragging ? 'transform 0.1s ease-out' : 'all 0.15s cubic-bezier(0.32, 0.72, 0, 1)'
          }}
        >
          <div className="w-1.5 h-1.5 rounded-full bg-black/25" />
        </div>
      </div>
    </div>
  );
};
