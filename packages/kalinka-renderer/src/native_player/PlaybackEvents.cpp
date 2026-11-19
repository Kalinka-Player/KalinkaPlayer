#include "PlaybackEvents.h"

#include <algorithm>

void PlaybackEvents::post(const PlaybackEvent &event) {
  {
    std::lock_guard lock(mutex);
    latest[event.kind.index()] = event;
  }
  posted.notify_one();
}

std::vector<PlaybackEvent> PlaybackEvents::take(std::stop_token token) {
  std::unique_lock lock(mutex);
  const auto any = [this] {
    return std::ranges::any_of(latest,
                               [](const auto &event) { return bool(event); });
  };
  posted.wait(lock, token, any);
  if (token.stop_requested())
    return {};
  std::vector<PlaybackEvent> events;
  for (auto &event : latest) {
    if (event) {
      events.push_back(*event);
      event.reset();
    }
  }
  return events;
}
