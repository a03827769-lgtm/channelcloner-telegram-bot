import React, { useState } from 'react';
import { ShieldCheck, Send, Copy, Check, ExternalLink, Lock } from 'lucide-react';

export const TelegramGuard: React.FC = () => {
  const [copied, setCopied] = useState(false);
  const botUsername = 'klonlabot';
  const botUrl = `https://t.me/${botUsername}`;

  const handleCopy = () => {
    navigator.clipboard.writeText(botUrl);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <div className="min-h-screen w-full flex items-center justify-center p-4 relative overflow-hidden bg-black text-white selection:bg-[#0A84FF]/30">
      {/* Apple Intelligence Chromatic Aurora Mesh */}
      <div className="apple-intelligence-aurora" />

      <div className="w-full max-w-sm relative z-10">
        {/* Main Apple Liquid Glass Card */}
        <div className="vision-glass-panel p-7 text-center relative overflow-hidden flex flex-col items-center">
          <div className="absolute top-0 inset-x-0 h-px bg-gradient-to-r from-transparent via-white/25 to-transparent" />

          {/* Telegram Brand Squircle */}
          <div className="relative w-20 h-20 mb-5 flex items-center justify-center">
            <div className="w-20 h-20 rounded-[22px] bg-gradient-to-b from-[#1A8CFF] to-[#0071E3] flex items-center justify-center shadow-[0_8px_24px_rgba(10,132,255,0.45)] border border-white/35">
              <Send className="w-9 h-9 text-white translate-x-[-1px] translate-y-[1px]" />
            </div>
            <div className="absolute -bottom-1 -right-1 w-6 h-6 rounded-full bg-black/90 border border-white/20 flex items-center justify-center shadow-lg">
              <Lock className="w-3 h-3 text-[#64D2FF]" />
            </div>
          </div>

          {/* Security Pill */}
          <div className="inline-flex items-center gap-1.5 px-3 py-1 rounded-full bg-blue-500/15 border border-blue-500/25 text-[#64D2FF] text-[11px] font-semibold mb-3">
            <ShieldCheck className="w-3.5 h-3.5" />
            <span>XAVFSIZ TELEGRAM HUDUDI</span>
          </div>

          <h1 className="text-[20px] font-bold tracking-tight text-white mb-2 leading-tight">
            Faqat Telegram Orqali Ochiladi
          </h1>

          <p className="text-[13px] text-white/65 leading-relaxed mb-5 font-normal">
            ChannelCloner Pro xavfsizlik va ma'lumotlar yaxlitligi sababli faqatgina rasmiy Telegram botimiz orqali ishga tushadi.
          </p>

          {/* Grouped Instructions */}
          <div className="w-full vision-grouped-list p-3.5 text-left mb-5 space-y-2.5">
            <div className="flex items-center gap-2.5 text-[12px] text-white/85">
              <span className="w-5 h-5 rounded-full bg-[#0A84FF]/25 text-[#64D2FF] font-bold flex items-center justify-center text-[10px] shrink-0 border border-[#0A84FF]/40">1</span>
              <span>Telegram ilovangizni oching</span>
            </div>
            <div className="flex items-center gap-2.5 text-[12px] text-white/85">
              <span className="w-5 h-5 rounded-full bg-[#0A84FF]/25 text-[#64D2FF] font-bold flex items-center justify-center text-[10px] shrink-0 border border-[#0A84FF]/40">2</span>
              <span><strong className="text-white font-semibold">@{botUsername}</strong> botiga kiring</span>
            </div>
            <div className="flex items-center gap-2.5 text-[12px] text-white/85">
              <span className="w-5 h-5 rounded-full bg-[#0A84FF]/25 text-[#64D2FF] font-bold flex items-center justify-center text-[10px] shrink-0 border border-[#0A84FF]/40">3</span>
              <span>Pastki menyudagi <strong className="text-[#64D2FF] font-semibold">"📱 Mini App"</strong> tugmasini bosing</span>
            </div>
          </div>

          {/* Action CTA Pill */}
          <a
            href={botUrl}
            target="_blank"
            rel="noopener noreferrer"
            className="w-full py-3.5 px-6 rounded-full vision-btn-blue text-white font-semibold text-[13px] active:scale-95 transition-transform flex items-center justify-center gap-2 mb-2.5"
          >
            <span>Telegram Botni Ochish</span>
            <ExternalLink className="w-3.5 h-3.5" />
          </a>

          {/* Secondary Action */}
          <button
            onClick={handleCopy}
            className="w-full py-2.5 px-4 rounded-full vision-btn-glass text-white/75 hover:text-white text-xs font-medium active:scale-95 transition-transform flex items-center justify-center gap-1.5"
          >
            {copied ? (
              <>
                <Check className="w-3.5 h-3.5 text-[#30D158]" />
                <span className="text-[#30D158] font-semibold">Nusxalandi!</span>
              </>
            ) : (
              <>
                <Copy className="w-3.5 h-3.5" />
                <span>Havolani nusxalash (t.me/{botUsername})</span>
              </>
            )}
          </button>
        </div>

        {/* Security Badge */}
        <div className="mt-5 text-center text-white/40 text-[11px] flex items-center justify-center gap-1">
          <ShieldCheck className="w-3.5 h-3.5" />
          <span>Telegram HMAC-SHA256 & 256-bit Shifrlash</span>
        </div>
      </div>
    </div>
  );
};
