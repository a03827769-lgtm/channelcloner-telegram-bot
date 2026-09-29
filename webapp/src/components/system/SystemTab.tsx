import React, { useState, useRef, useEffect } from 'react';
import { Server, Activity, Database, Cpu, Terminal, HelpCircle, ExternalLink, RefreshCw, Clock } from 'lucide-react';
import { SystemTelemetry } from '../../types';
import { telegram } from '../../services/telegram';

interface SystemTabProps {
  telemetry: SystemTelemetry | null;
  logs: string[];
  /** Host details and logs are only returned by the API to administrators */
  isAdmin: boolean;
  onRefresh: () => Promise<void>;
}

export const SystemTab: React.FC<SystemTabProps> = ({ telemetry, logs, isAdmin, onRefresh }) => {
  const [logFilter, setLogFilter] = useState<'all' | 'error' | 'info'>('all');
  const [isRefreshing, setIsRefreshing] = useState(false);
  const logContainerRef = useRef<HTMLDivElement>(null);

  const filteredLogs = logs.filter((log) => {
    if (logFilter === 'error') return log.includes('ERROR') || log.includes('WARN');
    if (logFilter === 'info') return log.includes('INFO');
    return true;
  });

  useEffect(() => {
    if (logContainerRef.current) {
      logContainerRef.current.scrollTop = logContainerRef.current.scrollHeight;
    }
  }, [filteredLogs.length]);

  const handleRefresh = async () => {
    setIsRefreshing(true);
    telegram.impact('light');
    try {
      await onRefresh();
    } finally {
      setIsRefreshing(false);
    }
  };

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center justify-between animate-fade-up">
        <div>
          <h2 className="text-[22px] font-bold text-white tracking-[-0.025em] flex items-center gap-2">
            <Server size={22} className="text-[#0A84FF]" />
            <span>Tizim Monitoringi</span>
          </h2>
          <p className="text-[13px] text-white/55 mt-0.5">
            24/7 background worker va server holati.
          </p>
        </div>

        <button
          onClick={handleRefresh}
          disabled={isRefreshing}
          className="vision-btn-glass p-2.5 text-white/70 hover:text-white transition-all active:scale-95"
          title="Yangilash"
        >
          <RefreshCw size={16} className={isRefreshing ? 'animate-spin text-[#0A84FF]' : ''} />
        </button>
      </div>

      {/* Telemetry visionOS Cards with Colored Squircles (From Image 4) */}
      <div className="grid grid-cols-2 gap-3">
        {/* MTProto worker */}
        <div className="vision-glass-panel p-4 flex flex-col justify-between min-h-[105px] animate-fade-up stagger-1">
          <div className="flex items-center justify-between">
            <span className="text-[11px] text-white/60 uppercase tracking-wide font-semibold">
              MTProto Worker
            </span>
            <div className={`apple-squircle-badge ${telemetry?.mtproto_connected ? 'bg-[#30D158]' : 'bg-[#FF453A]'} w-7 h-7 rounded-[8px]`}>
              <Activity size={13} className="text-white" />
            </div>
          </div>
          <div className="mt-2">
            <div className={`text-[14px] font-bold flex items-center gap-1.5 ${telemetry?.mtproto_connected ? 'text-[#30D158]' : 'text-[#FF453A]'}`}>
              <span className={`w-2 h-2 rounded-full animate-pulse ${telemetry?.mtproto_connected ? 'bg-[#30D158]' : 'bg-[#FF453A]'}`} />
              {telemetry?.mtproto_status || 'Ulanmoqda...'}
            </div>
          </div>
        </div>

        {/* Server clock */}
        <div className="vision-glass-panel p-4 flex flex-col justify-between min-h-[105px] animate-fade-up stagger-2">
          <div className="flex items-center justify-between">
            <span className="text-[11px] text-white/60 uppercase tracking-wide font-semibold">
              Server Vaqti
            </span>
            <div className="apple-squircle-badge bg-[#0A84FF] w-7 h-7 rounded-[8px]">
              <Clock size={13} className="text-white" />
            </div>
          </div>
          <div className="mt-2">
            <div className="text-[13px] font-bold text-white font-mono tabular-nums">
              {telemetry?.server_time || '—'}
            </div>
            <div className="text-[11px] text-[#0A84FF] mt-0.5 font-mono">
              Sinxronizatsiya faol
            </div>
          </div>
        </div>

        {isAdmin && (
          <>
            {/* API server (admin only) */}
            <div className="vision-glass-panel p-4 flex flex-col justify-between min-h-[105px] animate-fade-up stagger-3">
              <div className="flex items-center justify-between">
                <span className="text-[11px] text-white/60 uppercase tracking-wide font-semibold">
                  API Server
                </span>
                <div className="apple-squircle-badge bg-[#0A84FF] w-7 h-7 rounded-[8px]">
                  <Server size={13} className="text-white" />
                </div>
              </div>
              <div className="mt-2">
                <div className="text-[14px] font-bold text-white font-mono tabular-nums">
                  {telemetry?.keep_alive_port ? `0.0.0.0:${telemetry.keep_alive_port}` : '—'}
                </div>
                <div className="text-[11px] text-white/50 mt-0.5 font-mono truncate">
                  {telemetry?.runtime_version || '—'}
                </div>
              </div>
            </div>

            {/* Database Size (admin only) */}
            <div className="vision-glass-panel p-4 flex flex-col justify-between min-h-[105px] animate-fade-up stagger-4">
              <div className="flex items-center justify-between">
                <span className="text-[11px] text-white/60 uppercase tracking-wide font-semibold">
                  Baza Hajmi
                </span>
                <div className="apple-squircle-badge bg-[#64D2FF] w-7 h-7 rounded-[8px]">
                  <Database size={13} className="text-white" />
                </div>
              </div>
              <div className="mt-2">
                <div className="text-[14px] font-bold text-white font-mono tabular-nums">
                  {telemetry?.db_size_mb !== undefined ? `${telemetry.db_size_mb} MB` : '—'}
                </div>
                <div className="text-[11px] text-white/50 mt-0.5 font-mono truncate">
                  {telemetry?.db_type || '—'}
                </div>
              </div>
            </div>

            {/* RAM Usage (admin only) */}
            <div className="vision-glass-panel p-4 flex flex-col justify-between min-h-[105px] animate-fade-up stagger-5 col-span-2">
              <div className="flex items-center justify-between">
                <span className="text-[11px] text-white/60 uppercase tracking-wide font-semibold">
                  RAM Xotira
                </span>
                <div className="apple-squircle-badge bg-[#FF9F0A] w-7 h-7 rounded-[8px]">
                  <Cpu size={13} className="text-white" />
                </div>
              </div>
              <div className="mt-2">
                <div className="text-[14px] font-bold text-white font-mono tabular-nums">
                  {telemetry?.ram_mb != null ? `${telemetry.ram_mb} MB` : '—'}
                </div>
              </div>
            </div>
          </>
        )}
      </div>

      {/* Live Log Console (admin only) */}
      {isAdmin && (
        <div className="vision-glass-panel p-4 flex flex-col gap-3 animate-fade-up stagger-5">
          <div className="flex items-center justify-between">
            <span className="text-[13px] font-semibold text-white flex items-center gap-1.5">
              <Terminal size={14} className="text-[#0A84FF]" />
              Jonli Tizim Loglari
            </span>

            <div className="vision-segmented-container">
              {(['all', 'info', 'error'] as const).map((filter) => (
                <button
                  key={filter}
                  onClick={() => {
                    telegram.selection();
                    setLogFilter(filter);
                  }}
                  className={`vision-segment-pill text-[10px] py-0.5 px-2.5 font-mono capitalize ${
                    logFilter === filter ? 'active' : ''
                  }`}
                >
                  {filter}
                </button>
              ))}
            </div>
          </div>

          <div
            ref={logContainerRef}
            className="w-full h-44 vision-recessed-pod p-3 font-mono text-[11px] text-white/75 overflow-y-auto flex flex-col gap-1.5 select-text leading-relaxed"
          >
            {filteredLogs.length === 0 ? (
              <div className="text-white/40 my-auto text-center font-sans text-xs">Loglar mavjud emas</div>
            ) : (
              filteredLogs.map((log, idx) => {
                const isError = log.includes('ERROR') || log.includes('WARN');
                return (
                  <div
                    key={idx}
                    className={`leading-relaxed break-all ${
                      isError ? 'text-red-400 font-semibold' : 'text-white/75'
                    }`}
                  >
                    {log}
                  </div>
                );
              })
            )}
          </div>
        </div>
      )}

      {/* Support Link */}
      <button
        onClick={() => telegram.openLink('https://t.me/klonlabot')}
        className="vision-glass-panel p-4 flex items-center justify-between text-[13px] font-medium text-white/85 hover:text-white transition-all active:scale-[0.97] animate-fade-up stagger-6"
      >
        <span className="flex items-center gap-2.5">
          <div className="apple-squircle-badge bg-[#0A84FF] w-6 h-6 rounded-[6px]">
            <HelpCircle size={13} className="text-white" />
          </div>
          <span>Foydalanish Qo'llanmasi & Yordam</span>
        </span>
        <ExternalLink size={14} className="text-white/50" />
      </button>
    </div>
  );
};
