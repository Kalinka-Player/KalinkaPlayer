#include <gtest/gtest.h>

#include <memory>
#include <vector>

#include "capabilities/CapabilityService.h"
#include "fakes.h"

namespace pb = kalinka::renderer::v1;

namespace {

class RecordingListener : public CapabilityListener {
public:
  std::vector<bool> told;

  void onCapabilitiesChanged(const pb::Capabilities &capabilities) override {
    told.push_back(capabilities.dsd());
  }
};

/// Says when the storage it handed out is given back.
template <typename T> struct FreeingAllocator {
  using value_type = T;
  bool *freed;

  template <typename U>
  FreeingAllocator(const FreeingAllocator<U> &other) : freed(other.freed) {}
  explicit FreeingAllocator(bool *freed) : freed(freed) {}

  T *allocate(std::size_t n) { return std::allocator<T>{}.allocate(n); }
  void deallocate(T *p, std::size_t n) {
    *freed = true;
    std::allocator<T>{}.deallocate(p, n);
  }
  bool operator==(const FreeingAllocator &) const = default;
};

}  // namespace

class CapabilityServiceTest : public ::testing::Test {
protected:
  std::shared_ptr<FakeCapabilitySource> source =
      std::make_shared<FakeCapabilitySource>();
  CapabilityService service{source};
};

TEST_F(CapabilityServiceTest, TheAnswerIsWhatTheSourceSaysNow) {
  EXPECT_FALSE(service.current().dsd());
  source->dsd = true;
  EXPECT_TRUE(service.current().dsd());
}

TEST_F(CapabilityServiceTest, EveryListenerIsToldOfAChangeOnce) {
  auto first = std::make_shared<RecordingListener>();
  auto second = std::make_shared<RecordingListener>();
  service.addListener(first);
  service.addListener(second);

  service.refresh();
  source->dsd = true;
  service.refresh();
  service.refresh();

  EXPECT_EQ(first->told, std::vector<bool>{true});
  EXPECT_EQ(second->told, std::vector<bool>{true});
}

TEST_F(CapabilityServiceTest, TheAnswerAtStartIsNotAChange) {
  source->dsd = true;
  CapabilityService started{source};
  auto listener = std::make_shared<RecordingListener>();
  started.addListener(listener);

  started.refresh();

  EXPECT_TRUE(listener->told.empty());
}

TEST_F(CapabilityServiceTest, AListenerThatIsGoneIsNotKeptAlive) {
  auto gone = std::make_shared<RecordingListener>();
  auto staying = std::make_shared<RecordingListener>();
  service.addListener(gone);
  service.addListener(staying);
  const std::weak_ptr<RecordingListener> watched = gone;
  gone.reset();

  source->dsd = true;
  service.refresh();

  EXPECT_TRUE(watched.expired());
  EXPECT_EQ(staying->told, std::vector<bool>{true});
}

TEST_F(CapabilityServiceTest, AListenerThatIsGoneIsFreedWithoutAChange) {
  bool freed = false;
  auto gone = std::allocate_shared<RecordingListener>(
      FreeingAllocator<RecordingListener>{&freed});
  service.addListener(gone);
  gone.reset();
  ASSERT_FALSE(freed);

  service.addListener(std::make_shared<RecordingListener>());

  EXPECT_TRUE(freed);
}
