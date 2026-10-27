import base64
import hashlib
from pathlib import Path
import sys
from PIL import Image
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import dictate as m


def test_embedded_logo_is_exactly_the_packaged_artwork():
    root=Path(__file__).resolve().parents[1]
    assert base64.b64decode(m._LOGO_PNG)==(root/'assets/app-icon.png').read_bytes()
    icon=Image.open(root/'assets/app.ico')
    assert (256,256) in icon.ico.sizes()
    assert m._WINDOWS_APP_ID=='CometSuite.Dictate.Desktop'


def test_qt_application_icon_loads(qt):
    icon=m._application_icon()
    assert not icon.isNull()
    assert not icon.pixmap(32,32).isNull()


def test_new_logo_is_exactly_the_user_supplied_png():
    import json
    root=Path(__file__).resolve().parents[1]
    expected=json.loads((root/'tests/fixtures/branding_v122.json').read_text())
    assert hashlib.sha256((root/'assets/logo.png').read_bytes()).hexdigest()==expected['provided_logo_sha256']
    assert hashlib.sha256(base64.b64decode(m._LOGO_PNG)).hexdigest()==expected['embedded_icon_sha256']
    original=Image.open(root/'assets/logo.png').convert('RGBA')
    scale=256/max(original.size)
    scaled=original.resize((round(original.width*scale),round(original.height*scale)),Image.Resampling.LANCZOS)
    reference=Image.new('RGBA',(256,256),(0,0,0,0))
    reference.alpha_composite(scaled,((256-scaled.width)//2,(256-scaled.height)//2))
    assert Image.open(root/'assets/app-icon.png').convert('RGBA').tobytes()==reference.tobytes()


def test_logo_only_release_preserves_all_runtime_behavior():
    import ast
    import json
    root=Path(__file__).resolve().parents[1]
    expected=json.loads((root/'tests/fixtures/branding_v122.json').read_text())
    tree=ast.parse((root/'dictate.py').read_text())
    tree.body[0].value.value='<module documentation>'
    for node in tree.body:
        if not isinstance(node,ast.Assign):continue
        if len(node.targets)==1 and isinstance(node.targets[0],ast.Name) and node.targets[0].id=='_LOGO_PNG':
            node.value=ast.Constant(b'<artwork>')
        if (len(node.targets)==1 and isinstance(node.targets[0],ast.Tuple)
                and [getattr(item,'id',None) for item in node.targets[0].elts]==['APP_NAME','VERSION']):
            node.value.elts[1]=ast.Constant('<version>')
    # ast.dump formatting changed in Python 3.13 (empty fields are omitted).
    # Use an explicit canonical representation for identical 3.12/3.13 results.
    def canonical(value):
        if isinstance(value,ast.AST):
            return {'type':type(value).__name__,'fields':{
                name:canonical(item) for name,item in ast.iter_fields(value)
                if item is not None and item!=[]}}
        if isinstance(value,list):return [canonical(item) for item in value]
        if isinstance(value,bytes):return {'bytes':value.hex()}
        return value
    encoded=json.dumps(canonical(tree),sort_keys=True,separators=(',',':')).encode()
    digest=hashlib.sha256(encoded).hexdigest()
    assert digest==expected['runtime_ast_sha256'], 'Only version and logo may differ from v1.2.1'


def test_all_windows_icon_sizes_use_the_new_artwork():
    root=Path(__file__).resolve().parents[1]
    reference=Image.open(root/'assets/app-icon.png').convert('RGBA')
    icon=Image.open(root/'assets/app.ico')
    expected={(16,16),(24,24),(32,32),(48,48),(64,64),(128,128),(256,256)}
    assert icon.ico.sizes()==expected
    for size in expected:
        image=icon.ico.getimage(size).convert('RGBA')
        resized=reference.resize(size,Image.Resampling.LANCZOS)
        assert image.tobytes()==resized.tobytes(),size
