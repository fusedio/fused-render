fn main() {
    if std::env::var_os("CARGO_CFG_WINDOWS").is_some() {
        winresource::WindowsResource::new()
            .set_icon("../../fused_render/assets/fused-render.ico")
            .compile()
            .expect("could not embed the application icon");
    }
}
