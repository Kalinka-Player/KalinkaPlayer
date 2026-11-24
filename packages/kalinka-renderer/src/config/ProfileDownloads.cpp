#include "ProfileDownloads.h"
#include "DspProfileCache.h"
#include <boost/asio/post.hpp>

bool ProfileDownloads::prepare(
    const kalinka::renderer::v1::ConfigUpdate &update, Done done, bool fir) {
  if (downloading)
    return false;
  std::string id;
  bool selected = false;
  for (const auto &setting : update.settings()) {
    if (setting.path() != (fir ? "output.rew_fir_profile" : "dsp.profile"))
      continue;
    if (selected) {
      done("", "Select one DSP profile per update");
      return true;
    }
    selected = true;
    if (fir ? dsp_profiles::validId(setting.value())
            : setting.value() != "none")
      id = setting.value();
  }
  if (id.empty()) {
    done("", "");
    return true;
  }
  downloading = true;
  boost::asio::post(
      worker, [weak = weak_from_this(), work = boost::asio::make_work_guard(io),
               id = std::move(id), fir,
               url = fir ? update.fir_profile_url() : update.dsp_profile_url(),
               done = std::move(done)]() mutable {
        std::string error;
        try {
          dsp_profiles::fetch(id, url, fir);
        } catch (const std::exception &e) {
          error = e.what();
        }
        boost::asio::post(work.get_executor(),
                          [weak, id = std::move(id), error = std::move(error),
                           done = std::move(done)] {
                            if (auto self = weak.lock()) {
                              self->downloading = false;
                              done(id, error);
                            }
                          });
      });
  return true;
}
