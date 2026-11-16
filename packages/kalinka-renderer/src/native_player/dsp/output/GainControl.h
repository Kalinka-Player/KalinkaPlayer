#pragma once

/// @brief Where software volume is set: one linear gain the output heads for.
class GainControl {
public:
  virtual ~GainControl() = default;

  /// Sets the gain from any thread; 1.0 is unity, 0.0 silence.
  virtual void setGain(double linear) = 0;
};
