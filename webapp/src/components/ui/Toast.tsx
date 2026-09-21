import React from 'react';
import { CheckCircle2, AlertTriangle, XCircle, Info, X } from 'lucide-react';

export interface ToastMessage {
  id: string;
  type: 'success' | 'error' | 'warning' | 'info';
  text: string;
}

interface ToastProps {
  toasts: ToastMessage[];
  onDismiss: (id: string) => void;
}

export const ToastContainer: React.FC<ToastProps> = ({ toasts, onDismiss }) => {
  if (toasts.length === 0) return null;

  return (
    <div 
      className="fixed left-1/2 -translate-x-1/2 z-50 flex flex-col gap-2 w-full max-w-[350px] px-3 pointer-events-none"
      style={{
        top: 'calc(env(safe-area-inset-top, 0px) + 16px)'
      }}
    >
      {toasts.map((toast) => (
        <div
          key={toast.id}
          onClick={() => onDismiss(toast.id)}
          className={`pointer-events-auto flex items-center justify-between gap-3 px-4 py-3 rounded-full shadow-[0_12px_32px_rgba(0,0,0,0.85)] border backdrop-blur-3xl text-xs font-semibold animate-in fade-in slide-in-from-top-4 duration-200 cursor-pointer active:scale-95 transition-all ${
            toast.type === 'success'
              ? 'bg-[#121E16]/90 text-emerald-300 border-[#30D158]/35'
              : toast.type === 'error'
              ? 'bg-[#221214]/90 text-rose-300 border-[#FF453A]/35'
              : toast.type === 'warning'
              ? 'bg-[#221B0F]/90 text-[#FFD60A] border-[#FFD60A]/35'
              : 'bg-[#18191E]/90 text-white border-white/20'
          }`}
        >
          <div className="flex items-center gap-2.5 min-w-0">
            {toast.type === 'success' && (
              <div className="w-5 h-5 rounded-full bg-[#30D158]/20 flex items-center justify-center shrink-0">
                <CheckCircle2 size={13} className="text-[#30D158]" />
              </div>
            )}
            {toast.type === 'error' && (
              <div className="w-5 h-5 rounded-full bg-[#FF453A]/20 flex items-center justify-center shrink-0">
                <XCircle size={13} className="text-[#FF453A]" />
              </div>
            )}
            {toast.type === 'warning' && (
              <div className="w-5 h-5 rounded-full bg-[#FFD60A]/20 flex items-center justify-center shrink-0">
                <AlertTriangle size={13} className="text-[#FFD60A]" />
              </div>
            )}
            {toast.type === 'info' && (
              <div className="w-5 h-5 rounded-full bg-[#0A84FF]/20 flex items-center justify-center shrink-0">
                <Info size={13} className="text-[#0A84FF]" />
              </div>
            )}
            <span className="truncate leading-normal font-medium text-[12px]">{toast.text}</span>
          </div>

          <button
            type="button"
            onClick={(e) => {
              e.stopPropagation();
              onDismiss(toast.id);
            }}
            className="text-white/40 hover:text-white shrink-0 p-0.5"
          >
            <X size={13} />
          </button>
        </div>
      ))}
    </div>
  );
};
