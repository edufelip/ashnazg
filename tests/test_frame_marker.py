"""Validate the fixture's captured-frame count encoding contract on the host JVM."""
import pathlib
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'agent-android/fixture/src/main/java/dev/ashnazg/fixture/FrameMarkerCode.java'


@unittest.skipUnless(shutil.which('javac') and shutil.which('java'), 'JDK required')
class FrameMarkerCodeTests(unittest.TestCase):
    def test_unique_count_encoding_complements_and_input_bounds(self):
        with tempfile.TemporaryDirectory() as directory:
            work = pathlib.Path(directory)
            harness = work / 'FrameMarkerCodeTest.java'
            harness.write_text('''
import dev.ashnazg.fixture.FrameMarkerCode;
public class FrameMarkerCodeTest {
 public static void main(String[] args) {
  java.util.Set<Long> seen=new java.util.HashSet<>();
  for(int count=0;count<=65535;count++) {
   long pattern=0;
   for(int i=0;i<32;i++) {
    int color=FrameMarkerCode.cellColor(count,i);
    if(color!=0xff101010 && color!=0xfff0f0f0) throw new AssertionError("non-contract color");
    if(color==0xfff0f0f0)pattern|=1L<<i;
   }
   if((pattern&65535)!=(long)count)throw new AssertionError("count decoding mismatch");
   if((((pattern>>>16)^(pattern&65535))&65535)!=65535)throw new AssertionError("bad complement");
   if(!seen.add(pattern))throw new AssertionError("collision permits stale-frame acceptance");
  }
  for(int[] invalid:new int[][]{{-1,0},{65536,0},{0,-1},{0,32}}) {
   try {FrameMarkerCode.cellColor(invalid[0],invalid[1]);throw new AssertionError("invalid input accepted");}
   catch(IllegalArgumentException expected){}
  }
 }
}
''')
            compiled = subprocess.run([shutil.which('javac'), '-d', str(work), str(SOURCE), str(harness)], capture_output=True, text=True)
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            tested = subprocess.run([shutil.which('java'), '-cp', str(work), 'FrameMarkerCodeTest'], capture_output=True, text=True, timeout=15)
            self.assertEqual(tested.returncode, 0, tested.stderr)
