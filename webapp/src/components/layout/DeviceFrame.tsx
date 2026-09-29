import React from 'react';

interface DeviceFrameProps {
  children: React.ReactNode;
}

export const DeviceFrame: React.FC<DeviceFrameProps> = ({ children }) => {
  return (
    <div className="min-h-screen vision-spatial-canvas text-white flex flex-col relative w-full overflow-x-hidden selection:bg-[#0A84FF]/30 selection:text-white">
      {/* GPU-Accelerated 120 FPS Ambient Lighting (Zero layout thrashing) */}
      <div 
        className="fixed inset-0 pointer-events-none overflow-hidden z-0" 
        style={{ transform: 'translate3d(0,0,0)', contain: 'strict', willChange: 'transform' }}
      >
        <div className="absolute -top-24 left-1/2 -translate-x-1/2 w-[500px] h-[280px] bg-gradient-to-b from-amber-200/[0.08] to-transparent rounded-full opacity-60 pointer-events-none" />
        <div className="absolute top-1/4 -left-28 w-[320px] h-[380px] bg-gradient-to-tr from-[#0A84FF]/[0.10] to-transparent rounded-full opacity-50 pointer-events-none" />
        <div className="absolute top-1/2 -right-28 w-[300px] h-[360px] bg-gradient-to-tl from-[#BF5AF2]/[0.07] to-transparent rounded-full opacity-40 pointer-events-none" />
      </div>

      {/* Main Viewport Container with Dynamic Safe Area Inset Support */}
      <div 
        className="w-full max-w-md mx-auto flex-1 flex flex-col relative z-10 px-3.5"
        style={{
          paddingTop: 'calc(env(safe-area-inset-top, 0px) + 6px)',
          paddingBottom: 'calc(env(safe-area-inset-bottom, 0px) + 8px)',
          contain: 'layout style',
        }}
      >
        {children}
      </div>
    </div>
  );
};
