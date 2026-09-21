import React from 'react';

interface DeviceFrameProps {
  children: React.ReactNode;
}

export const DeviceFrame: React.FC<DeviceFrameProps> = ({ children }) => {
  return (
    <div className="min-h-screen vision-spatial-canvas text-white flex flex-col relative w-full overflow-x-hidden selection:bg-[#0A84FF]/30 selection:text-white">
      {/* visionOS Spatial Room Ambient Lighting (From Images 1 & 2) */}
      <div className="fixed inset-0 pointer-events-none overflow-hidden z-0">
        {/* Warm ceiling light — brighter, wider amber wash */}
        <div className="absolute -top-24 left-1/2 -translate-x-1/2 w-[600px] h-[350px] bg-gradient-to-b from-amber-200/[0.09] via-orange-100/[0.04] to-transparent rounded-full blur-[100px]" />
        {/* Cool blue ambient window glow on left */}
        <div className="absolute top-1/4 -left-32 w-[400px] h-[500px] bg-gradient-to-tr from-[#0A84FF]/[0.12] via-[#5E5CE6]/[0.05] to-transparent rounded-full blur-[110px]" />
        {/* Soft violet-rose bounce on right */}
        <div className="absolute top-1/2 -right-32 w-[380px] h-[470px] bg-gradient-to-tl from-[#BF5AF2]/[0.08] via-rose-400/[0.03] to-transparent rounded-full blur-[110px]" />
        {/* Warm floor bounce light */}
        <div className="absolute -bottom-20 left-1/3 w-[500px] h-[250px] bg-gradient-to-t from-amber-300/[0.04] to-transparent rounded-full blur-[90px]" />
      </div>

      {/* Main Viewport Container with Dynamic Safe Area Inset Support */}
      <div 
        className="w-full max-w-md mx-auto flex-1 flex flex-col relative z-10 px-3.5"
        style={{
          paddingTop: 'calc(env(safe-area-inset-top, 0px) + 6px)',
          paddingBottom: 'calc(env(safe-area-inset-bottom, 0px) + 8px)',
        }}
      >
        {children}
      </div>
    </div>
  );
};
