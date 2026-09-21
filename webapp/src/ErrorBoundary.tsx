import React, { Component, ErrorInfo, ReactNode } from 'react';
import { AlertTriangle, RefreshCw } from 'lucide-react';
import { telegram } from './services/telegram';

interface Props {
  children: ReactNode;
}

interface State {
  hasError: boolean;
  error: Error | null;
}

export class ErrorBoundary extends Component<Props, State> {
  public state: State = {
    hasError: false,
    error: null
  };

  public static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error };
  }

  public componentDidCatch(error: Error, errorInfo: ErrorInfo) {
    console.error('Uncaught error:', error, errorInfo);
    telegram.notification('error');
  }

  public render() {
    if (this.state.hasError) {
      return (
        <div className="min-h-screen bg-[#08090A] flex items-center justify-center p-6 text-white font-sans">
          <div className="w-full max-w-sm glass-card rounded-[32px] p-8 flex flex-col items-center text-center border border-white/10">
            <div className="w-16 h-16 rounded-full bg-red-500/20 flex items-center justify-center mb-6 border border-red-500/30">
              <AlertTriangle className="text-red-500" size={32} />
            </div>
            <h2 className="text-xl font-bold mb-3 tracking-tight">Xatolik Yuz Berdi</h2>
            <p className="text-sm text-gray-400 mb-8 leading-relaxed">
              Kechirasiz, tizimda kutilmagan xatolik yuz berdi. Iltimos, ilovani qayta ishga tushiring.
            </p>
            <div className="bg-black/30 p-4 rounded-xl w-full mb-8 text-left overflow-hidden">
              <p className="text-xs text-red-400 font-mono break-words truncate">
                {this.state.error?.message || 'Unknown error'}
              </p>
            </div>
            <button
              onClick={() => window.location.reload()}
              className="btn-spring w-full flex items-center justify-center gap-2 bg-ios-blue text-white py-4 rounded-2xl font-bold text-[15px]"
            >
              <RefreshCw size={18} />
              Qayta Yuklash
            </button>
          </div>
        </div>
      );
    }

    return this.props.children;
  }
}
