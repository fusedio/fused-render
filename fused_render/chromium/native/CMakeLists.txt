# FusedChromium.framework + the FusedRender Helper app, built against a CEF
# binary distribution (cef-builds.spotifycdn.com). Driven by build.sh, which
# fetches the distribution named in ../manifest.json and assembles the
# downloadable engine directory; this file only knows how to compile.
#
#   cmake -S . -B build -DCEF_ROOT=/path/to/cef_binary_… -DCMAKE_BUILD_TYPE=Release \
#         -DCMAKE_OSX_ARCHITECTURES=arm64
#   cmake --build build --config Release
cmake_minimum_required(VERSION 3.21)
project(FusedChromium LANGUAGES C CXX OBJC OBJCXX)

set(CEF_ROOT "" CACHE PATH "Path to the unpacked CEF binary distribution")
if(NOT CEF_ROOT)
  message(FATAL_ERROR "CEF_ROOT must point at the unpacked CEF binary distribution")
endif()
if(NOT APPLE)
  message(FATAL_ERROR "FusedChromium is macOS only")
endif()

# Deployment target: cef_variables.cmake pins it to what CEF was built for
# (12.0 on this branch); the plists say the same.
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
# CEF's cmake helpers: FindCEF sets CEF_INCLUDE_PATH, CEF_BINARY_DIR,
# CEF_LIBCEF_DLL_WRAPPER_PATH, the compiler flags CEF was built with, and the
# SET_*_TARGET_PROPERTIES macros used below.
list(APPEND CMAKE_MODULE_PATH "${CEF_ROOT}/cmake")
find_package(CEF REQUIRED)
# The static C++ wrapper around CEF's C API; on macOS it loads the framework
# at run time (cef_load_library) — nothing here links libcef directly.
add_subdirectory(${CEF_LIBCEF_DLL_WRAPPER_PATH} libcef_dll_wrapper)

set(FR_COMMON_SOURCES FRCefApp.cc FRCefApp.h)
set(FR_FRAMEWORK_SOURCES
    FRChromiumView.h FRChromiumInternal.h FRChromiumEngine.mm FRChromiumView.mm)

# ---- the framework -----------------------------------------------------------------
add_library(FusedChromium SHARED ${FR_FRAMEWORK_SOURCES} ${FR_COMMON_SOURCES})
SET_LIBRARY_TARGET_PROPERTIES(FusedChromium)
target_include_directories(FusedChromium PRIVATE ${CEF_INCLUDE_PATH} ${CMAKE_CURRENT_SOURCE_DIR})
target_link_libraries(FusedChromium PRIVATE libcef_dll_wrapper ${CEF_STANDARD_LIBS}
                      "-framework Cocoa" "-framework QuartzCore")
target_compile_options(FusedChromium PRIVATE -fobjc-arc -Wno-unused-parameter)
set_target_properties(FusedChromium PROPERTIES
    FRAMEWORK TRUE
    FRAMEWORK_VERSION A
    MACOSX_FRAMEWORK_IDENTIFIER io.fused.render.chromium
    MACOSX_FRAMEWORK_INFO_PLIST "${CMAKE_CURRENT_SOURCE_DIR}/Framework-Info.plist.in"
    PUBLIC_HEADER FRChromiumView.h
    OUTPUT_NAME FusedChromium
    # Loaded by absolute path from PyObjC; no rpath to the app bundle.
    INSTALL_NAME_DIR "@rpath")
# SET_LIBRARY_TARGET_PROPERTIES carries CEF's executable linker flags
# (-Wl,-pie) onto this dylib; the linker warns and moves on.

# ---- the helper (one build; build.sh copies it into the four variants) -------------
add_executable(FusedRenderHelper MACOSX_BUNDLE FRHelperMain.mm ${FR_COMMON_SOURCES})
SET_EXECUTABLE_TARGET_PROPERTIES(FusedRenderHelper)
target_include_directories(FusedRenderHelper PRIVATE ${CEF_INCLUDE_PATH} ${CMAKE_CURRENT_SOURCE_DIR})
target_link_libraries(FusedRenderHelper PRIVATE libcef_dll_wrapper ${CEF_STANDARD_LIBS})
target_compile_options(FusedRenderHelper PRIVATE -fobjc-arc -Wno-unused-parameter)
set_target_properties(FusedRenderHelper PROPERTIES
    OUTPUT_NAME "FusedRender Helper"
    MACOSX_BUNDLE_INFO_PLIST "${CMAKE_CURRENT_SOURCE_DIR}/Helper-Info.plist.in"
    MACOSX_BUNDLE_BUNDLE_NAME "FusedRender Helper"
    MACOSX_BUNDLE_GUI_IDENTIFIER io.fused.render.chromium.helper)
