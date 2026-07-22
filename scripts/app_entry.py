"""py2app's APP script. Executed directly as __main__ by the bundle's stub
launcher (Contents/MacOS/FusedRender) on the bundled interpreter — trivial by
design, all real logic stays in fused_render/app.py.
"""

from fused_render.app import main

main()
