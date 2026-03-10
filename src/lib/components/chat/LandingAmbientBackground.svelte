<div class="landing-ambient" aria-hidden="true">
	<div class="tone tone-light"></div>
	<div class="tone tone-dark"></div>

	<div class="blob blob-red blob-light blob-a"></div>
	<div class="blob blob-blue blob-light blob-b"></div>
	<div class="blob blob-red blob-dark blob-a"></div>
	<div class="blob blob-blue blob-dark blob-b"></div>

	<div class="mist mist-light"></div>
	<div class="mist mist-dark"></div>
</div>

<style>
	.landing-ambient {
		position: absolute;
		inset: 0;
		overflow: hidden;
		pointer-events: none;
		z-index: 0;
		isolation: isolate;
	}

	.tone,
	.blob,
	.mist {
		position: absolute;
		inset: -12%;
		transition: opacity 650ms ease;
		will-change: transform, opacity;
	}

	.tone-light {
		/* Use a real image background, but keep subtle tint/lighting via overlays. */
		background:
			linear-gradient(165deg, rgba(251, 253, 255, 0.78) 0%, rgba(246, 249, 255, 0.62) 58%, rgba(249, 251, 255, 0.74) 100%),
			url('/background-1.png');
		background-repeat: no-repeat;
		background-position: center;
		background-size: cover;
		opacity: 1;
	}

	.tone-dark {
		background:
			linear-gradient(
				165deg,
				rgba(6, 9, 18, 0.66) 0%,
				rgba(10, 14, 26, 0.60) 58%,
				rgba(12, 18, 34, 0.66) 100%
			),
			url('/background-1.png');
		background-repeat: no-repeat;
		background-position: center;
		background-size: cover;
		background-color: #0b1020;
		/* Darken the bright source image in dark mode (keeps it readable behind UI). */
		filter: brightness(0.68) saturate(0.95) contrast(1.06);
		opacity: 0;
	}

	.blob {
		border-radius: 999px;
		filter: blur(88px);
	}

	.blob-light {
		opacity: 0.28;
	}

	.blob-dark {
		opacity: 0;
	}

	.blob-red {
		background: radial-gradient(circle, rgba(239, 91, 109, 0.72) 0%, rgba(239, 91, 109, 0) 72%);
	}

	.blob-blue {
		background: radial-gradient(circle, rgba(76, 128, 255, 0.76) 0%, rgba(76, 128, 255, 0) 72%);
	}

	.blob-a {
		top: -6%;
		left: -14%;
		right: auto;
		bottom: auto;
		width: min(56vw, 760px);
		height: min(56vw, 760px);
		animation: ambient-drift-a 22s ease-in-out infinite alternate;
	}

	.blob-b {
		top: -12%;
		right: -18%;
		left: auto;
		bottom: auto;
		width: min(58vw, 820px);
		height: min(58vw, 820px);
		animation: ambient-drift-b 26s ease-in-out infinite alternate;
	}

	.mist-light {
		background: radial-gradient(circle at 50% 120%, rgba(255, 255, 255, 0.66), rgba(255, 255, 255, 0));
		opacity: 1;
	}

	.mist-dark {
		background: radial-gradient(circle at 50% 120%, rgba(12, 18, 32, 0.62), rgba(12, 18, 32, 0));
		opacity: 0;
	}

	:global(.dark) .tone-light,
	:global(.dark) .blob-light,
	:global(.dark) .mist-light {
		opacity: 0;
	}

	:global(.dark) .tone-dark,
	:global(.dark) .mist-dark {
		opacity: 1;
	}

	:global(.dark) .blob-dark {
		opacity: 0.22;
	}

	@keyframes ambient-drift-a {
		0% {
			transform: translate3d(0, 0, 0) scale(1) rotate(0deg);
		}
		33% {
			transform: translate3d(6%, 9%, 0) scale(1.06) rotate(1.5deg);
		}
		66% {
			transform: translate3d(12%, 4%, 0) scale(1.08) rotate(-1deg);
		}
		100% {
			transform: translate3d(14%, 7%, 0) scale(1.05) rotate(2deg);
		}
	}

	@keyframes ambient-drift-b {
		0% {
			transform: translate3d(0, 0, 0) scale(1) rotate(0deg);
		}
		33% {
			transform: translate3d(-7%, 8%, 0) scale(1.05) rotate(-1.2deg);
		}
		66% {
			transform: translate3d(-12%, 3%, 0) scale(1.07) rotate(1.4deg);
		}
		100% {
			transform: translate3d(-15%, 6%, 0) scale(1.04) rotate(-2deg);
		}
	}

	@media (max-width: 768px) {
		.blob {
			filter: blur(70px);
		}

		.blob-a,
		.blob-b {
			width: min(82vw, 620px);
			height: min(82vw, 620px);
		}
	}
</style>
