import { useId } from "react";

// The CortexMail app icon.
export function Mark({ className }: { className?: string }) {
  const uid = useId();
  const g = (n: number) => `${uid}-g${n}`;
  return (
    <svg
      className={className}
      viewBox="0 0 31 39"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      aria-hidden="true"
    >
      <path
        d="M0 3.81885L6.77675 38.9502L4.43846 15.4362L15.9949 14.6828L15.9734 10.7473L20.5341 7.62939e-06L16.1829 7.15864L0 3.81885Z"
        fill={`url(#${g(0)})`}
      />
      <path
        d="M18.4794 21.3234L20.5341 0.00279236L17.0538 15.8181L9.24406 16.2343L9.86925 24.3824L6.77675 38.9502L11.578 23.062L18.4794 21.3234Z"
        fill={`url(#${g(1)})`}
      />
      <path
        d="M19.8377 26.3544L20.0563 22.1919L13.6933 23.6872L13.6805 28.3849L6.77675 38.9502L15.41 27.9532L19.8377 26.3544ZM20.9969 24.4478L26.9067 22.5638L27.5076 17.5734L21.1764 18.4077L20.9969 24.4478ZM23.5291 17.323L27.9046 16.7587L28.5342 12.6659L25.317 12.5174L20.5341 7.62939e-06L23.891 12.3314L23.5291 17.323Z"
        fill={`url(#${g(2)})`}
      />
      <path
        d="M29.6388 12.0634L30.139 8.27801L26.3385 7.20523L20.5341 7.62939e-06L26.3014 8.93466L26.1334 11.7221L29.6388 12.0634Z"
        fill={`url(#${g(3)})`}
      />
      <defs>
        <linearGradient
          id={g(0)}
          x1="-0.652677"
          y1="2.14762"
          x2="17.9576"
          y2="10.8257"
          gradientUnits="userSpaceOnUse"
        >
          <stop stopColor="#913C00" />
          <stop offset="1" stopColor="#D9AD76" />
        </linearGradient>
        <linearGradient
          id={g(1)}
          x1="5.48796"
          y1="5.01412"
          x2="17.9564"
          y2="10.8282"
          gradientUnits="userSpaceOnUse"
        >
          <stop stopColor="#913C00" />
          <stop offset="1" stopColor="#D9AD76" />
        </linearGradient>
        <linearGradient
          id={g(2)}
          x1="5.48914"
          y1="5.0116"
          x2="25.2081"
          y2="14.2067"
          gradientUnits="userSpaceOnUse"
        >
          <stop stopColor="#913C00" />
          <stop offset="1" stopColor="#D9AD76" />
        </linearGradient>
        <linearGradient
          id={g(3)}
          x1="17.9576"
          y1="10.8257"
          x2="26.6625"
          y2="14.8849"
          gradientUnits="userSpaceOnUse"
        >
          <stop stopColor="#913C00" />
          <stop offset="1" stopColor="#D9AD76" />
        </linearGradient>
      </defs>
    </svg>
  );
}
