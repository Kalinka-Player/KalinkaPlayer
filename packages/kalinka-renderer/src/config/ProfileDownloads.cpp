#include "ProfileDownloads.h"
#include "DspProfileCache.h"
#include <boost/asio/post.hpp>

void ProfileDownloads::prepare(
    const kalinka::renderer::v1::ConfigUpdate &update, Done done) {
  Request request{.url = update.dsp_profile_url(), .done = std::move(done)};
  bool selected = false;
  for (const auto &setting : update.settings()) {
    if (setting.path() != "dsp.profile")
      continue;
    if (selected) {
      request.error = "Select one DSP profile per update";
      break;
    }
    selected = true;
    if (setting.value() != "none")
      request.id = setting.value();
  }
  queue.push_back(std::move(request));
  if (queue.size() == 1)
    next();
}

void ProfileDownloads::next() {
  while (!queue.empty() &&
         (queue.front().id.empty() || !queue.front().error.empty())) {
    queue.front().done("", queue.front().error);
    queue.pop_front();
  }
  if (queue.empty())
    return;
  boost::asio::post(worker, [weak = weak_from_this(),
                             work = boost::asio::make_work_guard(io),
                             id = queue.front().id, url = queue.front().url] {
    std::string error;
    try {
      dsp_profiles::fetch(id, url);
    } catch (const std::exception &e) {
      error = e.what();
    }
    boost::asio::post(work.get_executor(),
                      [weak, id, error = std::move(error)] {
                        if (auto self = weak.lock()) {
                          self->queue.front().done(id, error);
                          self->queue.pop_front();
                          self->next();
                        }
                      });
  });
}
