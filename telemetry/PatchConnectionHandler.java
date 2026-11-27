// Build-time hook for JOAL 2.1.x ConnectionHandler (v1.34).
// 1. fetchIp() no longer asks five public "what is my IP" sites at start-up and every 90 min:
//    no .client profile sends the address to trackers, so it returns the loopback address.
// 2. getPort() passes the bound port through UploadTelemetry.announcedPort(), which returns
//    -Dcontroller.port (the real qBittorrent listening port) when the controller sets it.
// No branch is added: the existing stack map frames stay valid.
import java.nio.file.*;
import jdk.internal.org.objectweb.asm.*;

public class PatchConnectionHandler {
    public static void main(String[] args) throws Exception {
        Path file = Path.of(args[0]);
        ClassReader input = new ClassReader(Files.readAllBytes(file));
        ClassWriter output = new ClassWriter(input, ClassWriter.COMPUTE_MAXS);
        int[] hooks = {0, 0};
        input.accept(new ClassVisitor(Opcodes.ASM7, output) {
            @Override public MethodVisitor visitMethod(int flags, String name, String desc, String sig, String[] ex) {
                MethodVisitor method = super.visitMethod(flags, name, desc, sig, ex);
                if (name.equals("fetchIp") && desc.equals("()Ljava/net/InetAddress;")) {
                    method.visitCode();
                    method.visitMethodInsn(Opcodes.INVOKESTATIC, "java/net/InetAddress", "getLoopbackAddress",
                        "()Ljava/net/InetAddress;", false);
                    method.visitInsn(Opcodes.ARETURN);
                    method.visitMaxs(1, 1);
                    method.visitEnd();
                    hooks[0]++;
                    return null;                       // original body (network calls) dropped
                }
                if (!name.equals("getPort") || !desc.equals("()I")) return method;
                return new MethodVisitor(Opcodes.ASM7, method) {
                    @Override public void visitInsn(int op) {
                        if (op == Opcodes.IRETURN) {
                            super.visitMethodInsn(Opcodes.INVOKESTATIC, "org/araymond/joal/telemetry/UploadTelemetry",
                                "announcedPort", "(I)I", false);
                            hooks[1]++;
                        }
                        super.visitInsn(op);
                    }
                };
            }
        }, 0);
        if (hooks[0] != 1 || hooks[1] == 0) throw new IllegalStateException("ConnectionHandler incompatible");
        Files.write(file, output.toByteArray());
    }
}
