using System;
using System.Runtime.InteropServices;

// BL pickfolder.exe: большой диалог выбора папки (IFileDialog + FOS_PICKFOLDERS).
// Сборка: csc v4.0.30319 /target:winexe? Нет — console, путь в stdout.
public static class BlFolderPicker
{
    [DllImport("shell32.dll", CharSet = CharSet.Unicode, PreserveSig = false)]
    private static extern void SHCreateItemFromParsingName(string pszPath, IntPtr pbc,
        [MarshalAs(UnmanagedType.LPStruct)] Guid riid, out IShellItemR ppv);

    [ComImport, Guid("DC1C5A9C-E88A-4dde-A5A1-60F82A20AEF7")]
    private class FileOpenDialogRCW { }

    [ComImport, Guid("42f85136-db7e-439c-85f1-e4075d135fc8"),
     InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IFileDialog
    {
        [PreserveSig] uint Show(IntPtr hwndOwner);
        void SetFileTypes(uint cFileTypes, IntPtr rgFilterSpec);
        void SetFileTypeIndex(uint iFileType);
        void GetFileTypeIndex(out uint piFileType);
        void Advise(IntPtr pfde, out uint pdwCookie);
        void Unadvise(uint dwCookie);
        void SetOptions(uint options);
        void GetOptions(out uint options);
        void SetDefaultFolder(IShellItemR psi);
        void SetFolder(IShellItemR psi);
        void GetFolder(out IShellItemR ppsi);
        void GetCurrentSelection(out IShellItemR ppsi);
        void SetFileName(string pszName);
        void GetFileName([MarshalAs(UnmanagedType.LPWStr)] out string pszName);
        void SetTitle(string pszTitle);
        void SetOkButtonLabel(string pszText);
        void SetFileNameLabel(string pszTextLabel);
        void GetResult(out IShellItemR ppsi);
        void AddPlace(IShellItemR psi, uint fdap);
        void SetDefaultExtension(string pszDefaultExtension);
        void Close(int hr);
        void SetClientGuid(ref Guid guid);
        void ClearClientData();
        void SetFilter(IntPtr pFilter);
    }

    [ComImport, Guid("43826d1e-e718-42ee-bc55-a1e261c37bfe"),
     InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IShellItemR
    {
        void BindToHandler(IntPtr pbc, ref Guid bhid, ref Guid riid, out IntPtr ppv);
        void GetParent(out IShellItemR ppsi);
        void GetDisplayName(uint sigdnName, [MarshalAs(UnmanagedType.LPWStr)] out string ppszName);
        void GetAttributes(uint sfgaoMask, out uint psfgaoAttribs);
        void Compare(IShellItemR psi, uint hint, out int piOrder);
    }

    public static int Main(string[] args)
    {
        string title = args.Length > 0 && args[0].Length > 0 ? args[0] : "Выбрать папку";
        string initial = args.Length > 1 ? args[1] : "";
        try
        {
            IFileDialog dialog = (IFileDialog)new FileOpenDialogRCW();
            dialog.SetOptions(0x20u | 0x40u | 0x8u);  // PICKFOLDERS | FORCEFILESYSTEM | PATHMUSTEXIST
            dialog.SetTitle(title);
            if (initial.Length > 0)
            {
                try
                {
                    Guid iid = typeof(IShellItemR).GUID;
                    IShellItemR item;
                    SHCreateItemFromParsingName(initial, IntPtr.Zero, iid, out item);
                    if (item != null) { dialog.SetFolder(item); }
                }
                catch { }
            }
            if (dialog.Show(IntPtr.Zero) == 0)
            {
                IShellItemR result;
                dialog.GetResult(out result);
                string path;
                result.GetDisplayName(0x80018000u, out path);  // SIGDN_DESKTOPABSOLUTEEDITING
                if (path != null && path.Length > 0)
                {
                    Console.OutputEncoding = System.Text.Encoding.UTF8;
                    Console.Out.WriteLine(path);
                }
            }
            return 0;
        }
        catch (Exception e)
        {
            Console.Error.WriteLine(e.Message);
            return 1;
        }
    }
}
