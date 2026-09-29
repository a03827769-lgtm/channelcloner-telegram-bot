import React from 'react';
import { Eye, Forward, Globe } from 'lucide-react';

interface PostPreviewSimulatorProps {
  sourceTitle: string;
  targetTitle: string;
  cloneMode: 'clean' | 'forward';
  signature: string;
  watermarkType: 'none' | 'text' | 'logo';
  watermarkText: string;
  watermarkPos: string;
  targetLang?: string;
  autoTranslate?: boolean;
}

export const PostPreviewSimulator: React.FC<PostPreviewSimulatorProps> = ({
  sourceTitle,
  targetTitle,
  cloneMode,
  signature,
  watermarkType,
  watermarkText,
  watermarkPos,
  targetLang,
  autoTranslate
}) => {
  // Map 9-grid position to Tailwind classes
  const getPosClasses = (pos: string) => {
    switch (pos) {
      case 'top_left': return 'top-2.5 left-2.5';
      case 'top_center': return 'top-2.5 left-1/2 -translate-x-1/2';
      case 'top_right': return 'top-2.5 right-2.5';
      case 'center_left': return 'top-1/2 -translate-y-1/2 left-2.5';
      case 'center': return 'top-1/2 left-1/2 -translate-x-1/2 -translate-y-1/2';
      case 'center_right': return 'top-1/2 -translate-y-1/2 right-2.5';
      case 'bottom_left': return 'bottom-2.5 left-2.5';
      case 'bottom_center': return 'bottom-2.5 left-1/2 -translate-x-1/2';
      case 'bottom_right':
      default: return 'bottom-2.5 right-2.5';
    }
  };

  return (
    <div className="w-full shrink-0 rounded-[22px] bg-black/40 p-3.5 border border-white/10 shadow-xl flex flex-col gap-2.5 backdrop-blur-md">
      <div className="flex items-center justify-between text-[11px] text-white/60 border-b border-white/[0.08] pb-2">
        <span className="font-semibold text-[#64D2FF] flex items-center gap-1.5">
          <Eye size={13} /> Telegram Xabar Ko'rinishi (Jonli Simulyatsiya)
        </span>
        <span className="text-[10px] text-white/40 font-mono">Nishon kanal</span>
      </div>

      {/* Telegram Message Bubble with Apple visionOS Specular Depth */}
      <div className="w-full max-w-[340px] mx-auto rounded-[20px] bg-[#182533]/90 border border-white/10 p-3.5 text-white shadow-lg flex flex-col gap-2.5">
        {/* Forward Header if cloneMode == 'forward' */}
        {cloneMode === 'forward' && (
          <div className="flex items-center gap-1.5 text-[11px] text-[#64D2FF] font-medium border-l-2 border-[#0A84FF] pl-2 py-0.5">
            <Forward size={13} />
            <span>Forwarded from <b>{sourceTitle || 'Manba Kanal'}</b></span>
          </div>
        )}

        {/* Media Preview Box */}
        <div className="relative w-full h-36 rounded-[14px] overflow-hidden bg-gradient-to-br from-[#1C2C3D] to-[#121D28] border border-white/10 flex items-center justify-center">
          <img
            src="https://images.unsplash.com/photo-1600585154340-be6161a56a0c?auto=format&fit=crop&w=600&q=80"
            alt="Sample Post"
            className="w-full h-full object-cover opacity-85"
          />

          {/* Watermark Overlay Badge */}
          {watermarkType !== 'none' && (
            <div
              className={`absolute ${getPosClasses(watermarkPos)} px-2 py-1 rounded-[8px] bg-black/75 backdrop-blur-md text-[10px] font-bold text-white tracking-wide border border-white/20 shadow-lg flex items-center gap-1.5`}
            >
              {watermarkType === 'text' ? (
                <span>{watermarkText || targetTitle || 'WATERMARK'}</span>
              ) : (
                <span className="flex items-center gap-1">
                  <span className="w-2 h-2 rounded-full bg-[#0A84FF]" /> LOGO
                </span>
              )}
            </div>
          )}
        </div>

        {/* Message Caption */}
        <div className="text-[12px] text-white/85 leading-relaxed font-normal">
          {autoTranslate && targetLang && (
            <span className="inline-flex items-center gap-1 mb-1 px-1.5 py-0.5 rounded-full bg-[#0A84FF]/15 text-[#64D2FF] text-[10px] font-semibold">
              <Globe size={10} /> {targetLang.toUpperCase()} tarjima
            </span>
          )}
          {autoTranslate ? (
            <p>
              Toshkent markazida hashamatli yangi xonadon sotuvga qo'yildi! Barcha qulayliklar mavjud, yevro ta'mir.
            </p>
          ) : (
            <p>
              Luxury modern apartment in the heart of Tashkent is now available! Premium furnishing, prime location.
            </p>
          )}

          {/* Cleaned Signature / Custom Signature */}
          {signature && (
            <p className="mt-2 text-[#64D2FF] font-medium text-[11px] border-t border-white/10 pt-1 flex items-center gap-1">
              <span>{signature}</span>
            </p>
          )}
        </div>

        {/* Message Timestamp and Read receipt */}
        <div className="flex items-center justify-end gap-1 text-[10px] text-white/40 font-mono pt-1">
          <span>12:45</span>
          <span className="text-[#30D158]">✓✓</span>
        </div>
      </div>
    </div>
  );
};
