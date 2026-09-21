import React from 'react';
import { telegram } from '../../services/telegram';

interface SwitchProps {
  checked: boolean;
  onChange: (checked: boolean) => void;
  disabled?: boolean;
}

export const Switch: React.FC<SwitchProps> = ({ checked, onChange, disabled }) => {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      disabled={disabled}
      onClick={() => {
        if (!disabled) {
          telegram.impact('light');
          onChange(!checked);
        }
      }}
      className={`relative inline-flex h-[31px] w-[51px] shrink-0 cursor-pointer items-center rounded-full p-[2px] transition-all duration-300 focus:outline-none ${
        checked ? 'bg-[#30D158]' : 'bg-[rgba(120,120,128,0.32)]'
      } ${disabled ? 'opacity-40 cursor-not-allowed' : 'active:scale-95'}`}
      style={{ transitionTimingFunction: 'cubic-bezier(0.34, 1.56, 0.64, 1)' }}
    >
      <span
        aria-hidden="true"
        className={`pointer-events-none inline-block h-[27px] w-[27px] rounded-full bg-white transition-all duration-300 ${
          checked ? 'translate-x-[20px] shadow-[0_3px_8px_rgba(0,0,0,0.35),0_1px_1px_rgba(0,0,0,0.15),0_0_6px_rgba(52,199,89,0.3)]' : 'translate-x-0 shadow-[0_3px_8px_rgba(0,0,0,0.35),0_1px_1px_rgba(0,0,0,0.15)]'
        }`}
        style={{ transitionTimingFunction: 'cubic-bezier(0.34, 1.56, 0.64, 1)' }}
      />
    </button>
  );
};
