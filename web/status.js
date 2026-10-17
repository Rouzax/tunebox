const root = document.getElementById("channels");

function trackItem(track, prefix) {
  const item = document.createElement("li");
  const image = document.createElement("img");
  image.alt = "";
  if (track.cover) image.src = track.cover;
  const text = document.createElement("span");
  text.textContent = `${prefix}${track.artist} - ${track.title}` +
    (track.played_by ? ` (Played by ${track.played_by})` : "");
  item.append(image, text);
  return item;
}

function textItem(text) {
  const item = document.createElement("li");
  item.textContent = text;
  return item;
}

async function refresh() {
  try {
    const channels = await (await fetch("/api/channels")).json();
    const sections = await Promise.all(channels.map(async (channel) => {
      const now = await (await fetch(`/api/channels/${encodeURIComponent(channel.id)}/now`)).json();
      const section = document.createElement("section");
      const heading = document.createElement("h2");
      heading.textContent = channel.name + (now.stale_since ? " (feed unreachable)" : "");
      const list = document.createElement("ul");
      list.append(now.current ? trackItem(now.current, "") : textItem("Station ID"));
      if (now.upcoming) {
        const next = now.upcoming.track
          ? trackItem(now.upcoming.track, `in ${now.upcoming.in_s} s: `)
          : textItem(`in ${now.upcoming.in_s} s: station ID`);
        next.classList.add("dim");
        list.append(next);
      }
      for (const track of now.history.slice(0, 5)) {
        const item = trackItem(track, "");
        item.classList.add("dim");
        list.append(item);
      }
      section.append(heading, list);
      return section;
    }));
    root.textContent = "";
    root.append(...sections);
  } catch (error) {
    root.textContent = "";
    root.append(textItem(`Could not reach the API: ${error}`));
  }
}

refresh();
setInterval(refresh, 10000);
