const clock = document.querySelector("#videoClock");
let seconds = 0;

function tick() {
  seconds += 1;
  const minutes = Math.floor(seconds / 60).toString().padStart(2, "0");
  const rest = (seconds % 60).toString().padStart(2, "0");
  if (clock) clock.textContent = `${minutes}:${rest}`;
}

setInterval(tick, 1000);

