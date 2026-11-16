#include <gtest/gtest.h>

#include <cstdlib>
#include <string_view>

namespace {
/**
 * @brief Keeps every test off a real device's mixer, however the suite is run.
 *
 * Software and fixed volume raise the mixer to 100 % first. On a desktop's own
 * device that turns up everything else it plays, and leaves it there.
 */
class LeaveTheMixerAlone : public ::testing::Environment {
public:
  void SetUp() override { setenv("KALINKA_LEAVE_MIXER", "1", 1); }
};

// gtest_main runs the tests, so the environment registers before main().
[[maybe_unused]] const ::testing::Environment *const kLeaveTheMixerAlone =
    ::testing::AddGlobalTestEnvironment(new LeaveTheMixerAlone);
} // namespace

TEST(TestEnvironment, NoTestRaisesARealMixer) {
  const char *value = std::getenv("KALINKA_LEAVE_MIXER");
  ASSERT_NE(value, nullptr);
  EXPECT_EQ(std::string_view(value), "1");
}
